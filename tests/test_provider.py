from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from longwatch.provider import LongbridgeProvider


class ProviderConversionTests(TestCase):
    def test_requires_oauth_client_id(self):
        with self.assertRaisesRegex(ValueError, "仅支持 OAuth"):
            LongbridgeProvider(oauth_client_id="")

    def test_uses_newest_extended_hours_quote(self):
        provider = LongbridgeProvider.__new__(LongbridgeProvider)
        provider._include_extended_hours = True
        raw = SimpleNamespace(
            symbol="AAPL.US",
            timestamp=100,
            last_done=Decimal("100"),
            prev_close=Decimal("98"),
            pre_market_quote=None,
            post_market_quote=SimpleNamespace(
                timestamp=110,
                last_done=Decimal("101"),
                prev_close=Decimal("100"),
            ),
            over_night_quote=None,
        )
        quote = provider._convert_quote(raw)
        self.assertEqual(quote.last_price, Decimal("101"))
        self.assertEqual(quote.previous_close, Decimal("100"))
        self.assertEqual(quote.session, "盘后")

    def test_accepts_datetime_timestamp_from_current_sdk(self):
        provider = LongbridgeProvider.__new__(LongbridgeProvider)
        provider._include_extended_hours = False
        raw = SimpleNamespace(
            symbol="NVDA.US",
            timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc),
            last_done=Decimal("200.75"),
            prev_close=Decimal("199.01"),
        )
        quote = provider._convert_quote(raw)
        self.assertEqual(quote.timestamp, 1785542400)

    def test_get_last_executions_selects_latest_and_side(self):
        provider = LongbridgeProvider.__new__(LongbridgeProvider)
        older = SimpleNamespace(
            symbol="AAPL.US", order_id="1", trade_done_at=100,
            price=Decimal("180"), quantity=Decimal("1"),
        )
        newer = SimpleNamespace(
            symbol="AAPL.US", order_id="2", trade_done_at=200,
            price=Decimal("190"), quantity=Decimal("2"),
        )
        provider._trade = SimpleNamespace(
            today_orders=lambda: [
                SimpleNamespace(order_id="1", side="OrderSide.Buy"),
                SimpleNamespace(order_id="2", side="OrderSide.Sell"),
            ],
            today_executions=lambda: [older, newer],
            history_orders=lambda **kwargs: [],
            history_executions=lambda **kwargs: [],
            order_detail=lambda order_id: SimpleNamespace(side="OrderSide.Buy"),
        )
        result = provider.get_last_executions(["AAPL.US"])
        self.assertEqual(result["AAPL.US"].last_trade.price, Decimal("190"))
        self.assertEqual(result["AAPL.US"].last_trade.side, "卖出")
        self.assertEqual(result["AAPL.US"].last_buy.price, Decimal("180"))
        self.assertEqual(result["AAPL.US"].last_buy.quantity, Decimal("1"))

    def test_get_today_last_executions_does_not_fall_back_to_history(self):
        provider = LongbridgeProvider.__new__(LongbridgeProvider)
        older = SimpleNamespace(
            symbol="AAPL.US", order_id="1", trade_done_at=100,
            price=Decimal("180"), quantity=Decimal("1"),
        )
        newer = SimpleNamespace(
            symbol="AAPL.US", order_id="2", trade_done_at=200,
            price=Decimal("190"), quantity=Decimal("2"),
        )
        provider._trade = SimpleNamespace(
            today_orders=lambda: [
                SimpleNamespace(order_id="1", side="OrderSide.Buy"),
                SimpleNamespace(order_id="2", side="OrderSide.Sell"),
            ],
            today_executions=lambda: [older, newer],
            history_orders=lambda **kwargs: self.fail("不应读取历史订单"),
            history_executions=lambda **kwargs: self.fail("不应读取历史成交"),
        )
        result = provider.get_today_last_executions(["AAPL.US"])
        self.assertEqual(result["AAPL.US"].price, Decimal("190"))
        self.assertEqual(result["AAPL.US"].side, "卖出")

    @patch("longwatch.provider.subprocess.run")
    def test_get_news_parses_cli_json_and_classifies_title(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout='[{"id":"42","title":"公司发布最新季度财报","published_at":"2026-08-01T04:04:41Z","url":"https://longbridge.cn/news/42","comments_count":2,"likes_count":3}]',
        )
        provider = LongbridgeProvider.__new__(LongbridgeProvider)
        articles = provider.get_news("AAPL.US", count=5)
        self.assertEqual(articles[0].article_id, "42")
        self.assertEqual(articles[0].category, "catalyst")
        self.assertEqual(articles[0].category_label, "业绩")
        self.assertEqual(articles[0].comments_count, 2)
        run.assert_called_once()
