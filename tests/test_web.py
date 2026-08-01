import json
import threading
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from longwatch.alert_links import AlertLinkSigner
from longwatch.models import (
    Candlestick,
    ExecutionReferences,
    LastExecution,
    NewsArticle,
    Position,
    Quote,
)
from longwatch.web import _decision_signal, create_server


class FakeProvider:
    def __init__(self):
        self.execution_calls = 0
        self.analytics_calls = 0
        self.news_calls = 0

    def get_positions(self):
        return [Position("AAPL.US", "Apple", Decimal("3"), Decimal("180.5"), "USD")]

    def get_candlesticks(self, **kwargs):
        if kwargs.get("count") == 61:
            self.analytics_calls += 1
            return [
                Candlestick(
                    timestamp=1_690_000_000 + index * 86400,
                    open=Decimal(100 + index),
                    high=Decimal(101 + index),
                    low=Decimal(99 + index),
                    close=Decimal(100 + index),
                    volume=2000 if index == 60 else 1000,
                    turnover=Decimal("100000"),
                )
                for index in range(61)
            ]
        return [
            Candlestick(
                timestamp=1_700_000_000,
                open=Decimal("180"),
                high=Decimal("184"),
                low=Decimal("179"),
                close=Decimal("183"),
                volume=123456,
                turnover=Decimal("22500000"),
            )
        ]

    def get_quotes(self, symbols, include_extended_hours=None):
        return {
            symbol: Quote(
                symbol,
                Decimal("89.228") if symbol == "INTC.US" else Decimal("205.25"),
                Decimal("90") if symbol == "INTC.US" else Decimal("200"),
                1_700_000_100,
                "盘后" if include_extended_hours else "常规",
            )
            for symbol in symbols
        }

    def get_last_executions(self, symbols):
        self.execution_calls += 1
        return {
            "AAPL.US": ExecutionReferences(
                last_trade=LastExecution(
                    "AAPL.US", Decimal("195"), Decimal("1"), 1_699_100_000, "卖出"
                ),
                last_buy=LastExecution(
                    "AAPL.US", Decimal("190"), Decimal("2"), 1_699_000_000, "买入"
                ),
            )
        }

    def get_news(self, symbol, count=10):
        self.news_calls += 1
        return [
            NewsArticle(
                article_id="123",
                title="Apple 发布最新季度财报",
                published_at="2026-08-01T04:04:41Z",
                url="https://longbridge.cn/news/123",
                category="catalyst",
                category_label="业绩",
            )
        ]


class WebServerTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.env_path = Path(self.tempdir.name) / ".env"
        self.env_path.write_text(
            "BARK_SERVER_URL=https://api.day.app\n"
            "BARK_GROUP=LongWatch\n"
            "BARK_LEVEL=timeSensitive\n"
            "BARK_SOUND=\n"
            "ALERT_DETAIL_BASE_URL=http://10.0.0.2:8765\n",
            encoding="utf-8",
        )
        self.provider = FakeProvider()
        self.server = create_server(
            "127.0.0.1",
            0,
            lambda: self.provider,
            env_path=self.env_path,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.alert_secret = next(
            line.split("=", 1)[1]
            for line in self.env_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("ALERT_LINK_SECRET=")
        )
        self.alert_signer = AlertLinkSigner(self.alert_secret)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def get_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response.status, json.loads(response.read())

    def post_json(self, path, payload, include_header=True):
        headers = {"Content-Type": "application/json"}
        if include_header:
            headers["X-LongWatch-Request"] = "settings"
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_health_and_static_page(self):
        status, payload = self.get_json("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        with urlopen(f"{self.base_url}/", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertIn("LongWatch · 持仓 K 线", html)
        self.assertIn("LATEST NEWS", html)

    def test_alert_detail_requires_generated_token(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base_url}/?symbol=AAPL.US&view=alert", timeout=2)
        self.assertEqual(caught.exception.code, 403)

        token = self.alert_signer.issue("AAPL.US")
        with urlopen(
            f"{self.base_url}/?symbol=AAPL.US&view=alert&token={token}",
            timeout=2,
        ) as response:
            self.assertEqual(response.status, 200)

        with urlopen(
            f"{self.base_url}/?symbol=AAPL.US&view=portfolio", timeout=2
        ) as response:
            self.assertEqual(response.status, 200)

    def test_positions(self):
        status, payload = self.get_json("/api/positions")
        self.assertEqual(status, 200)
        self.assertEqual(payload["positions"][0]["symbol"], "AAPL.US")
        self.assertEqual(payload["positions"][0]["cost_price"], 180.5)
        self.assertEqual(payload["positions"][0]["current_price"], 205.25)
        self.assertEqual(payload["positions"][0]["day_change_pct"], 2.625)
        self.assertEqual(payload["positions"][0]["last_trade_price"], 195.0)
        self.assertEqual(payload["positions"][0]["last_trade_side"], "卖出")
        self.assertEqual(payload["positions"][0]["price_vs_last_trade"], 10.25)
        self.assertAlmostEqual(payload["positions"][0]["price_vs_last_trade_pct"], 5.256410256)
        self.assertEqual(payload["positions"][0]["last_buy_price"], 190.0)
        self.assertEqual(payload["positions"][0]["price_vs_last_buy"], 15.25)
        self.assertAlmostEqual(payload["positions"][0]["price_vs_last_buy_pct"], 8.026315789)
        self.assertEqual(payload["positions"][0]["currency_weight_pct"], 100.0)
        self.assertAlmostEqual(payload["positions"][0]["relative_change_pct"], 0.0)
        self.assertAlmostEqual(payload["positions"][0]["ma20"], 150.5)
        self.assertAlmostEqual(payload["positions"][0]["ma60"], 130.5)
        self.assertEqual(payload["positions"][0]["volume_ratio_20d"], 2.0)
        self.assertEqual(payload["positions"][0]["decision_signal"], "add_watch")

        _, extended = self.get_json("/api/positions?sessions=all")
        self.assertEqual(extended["positions"][0]["session"], "盘后")
        self.assertEqual(self.provider.execution_calls, 1)
        self.assertEqual(self.provider.analytics_calls, 1)

    def test_candlesticks(self):
        status, payload = self.get_json(
            "/api/candlesticks?symbol=AAPL.US&period=day&count=200&adjust=forward"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["candles"][0]["close"], 183.0)

    def test_news_and_cache(self):
        for _ in range(2):
            status, payload = self.get_json("/api/news?symbol=AAPL.US&count=5")
            self.assertEqual(status, 200)
            self.assertEqual(payload["articles"][0]["id"], "123")
            self.assertEqual(payload["articles"][0]["category"], "catalyst")
        self.assertEqual(self.provider.news_calls, 1)

    def test_bark_settings_are_saved_and_device_key_is_masked(self):
        status, initial = self.get_json("/api/settings/bark")
        self.assertEqual(status, 200)
        self.assertFalse(initial["configured"])
        self.assertNotIn("device_key", initial)

        status, saved = self.post_json(
            "/api/settings/bark",
            {
                "device_key": "secret-device-key",
                "server_url": "https://api.day.app",
                "group": "LongWatch 手机",
                "level": "timeSensitive",
                "sound": "alarm",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["configured"])
        self.assertNotIn("device_key", saved)
        contents = self.env_path.read_text(encoding="utf-8")
        self.assertIn("BARK_DEVICE_KEY=secret-device-key", contents)
        self.assertIn("BARK_GROUP=LongWatch 手机", contents)

        with patch("longwatch.web.BarkNotifier.send") as send:
            status, result = self.post_json("/api/settings/bark/test", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        send.assert_called_once()
        title, body, detail_url = send.call_args.args
        self.assertEqual(title, "【测试】INTC 快速下跌 -3.20%")
        self.assertEqual(
            body,
            "INTC.US｜盘后\n"
            "实时价格 89.228 USD\n"
            "当日涨跌 -0.86%\n"
            "模拟触发：5分钟快速下跌 -3.20%\n"
            "这是测试通知，不会记录为真实告警。",
        )
        parsed = urlsplit(detail_url)
        query = parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "http://10.0.0.2:8765/")
        self.assertEqual(query["symbol"], ["INTC.US"])
        self.assertEqual(query["view"], ["alert"])
        self.assertTrue(self.alert_signer.verify(query["token"][0], "INTC.US"))

    def test_bark_settings_post_requires_custom_header(self):
        with self.assertRaises(HTTPError) as caught:
            self.post_json(
                "/api/settings/bark",
                {"device_key": "secret"},
                include_header=False,
            )
        self.assertEqual(caught.exception.code, 403)

    def test_rejects_invalid_symbol_and_count(self):
        for path in (
            "/api/candlesticks?symbol=../../secret&period=day&count=200",
            "/api/candlesticks?symbol=AAPL.US&period=day&count=1001",
            "/api/news?symbol=AAPL.US&count=11",
        ):
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"{self.base_url}{path}", timeout=2)
            self.assertEqual(caught.exception.code, 400)

    def test_reduce_watch_signal(self):
        signal, label, reasons = _decision_signal(
            day_change=-4,
            relative_change=-2,
            current_price=80,
            metrics={
                "ma20": 90,
                "ma60": 100,
                "volume_ratio_20d": 2,
                "annualized_volatility_20d": 50,
            },
        )
        self.assertEqual(signal, "reduce_watch")
        self.assertEqual(label, "减仓观察")
        self.assertIn("放量下跌 2.0 倍", reasons)
