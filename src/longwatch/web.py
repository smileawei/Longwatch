from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from .alert_links import AlertLinkSigner
from .bark import BarkNotifier
from .config import ensure_alert_link_secret

logger = logging.getLogger(__name__)

PERIODS = {"1m", "5m", "15m", "30m", "60m", "day", "week", "month"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
BARK_LEVELS = {"active", "timeSensitive", "passive", "critical"}


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _write_dotenv_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    output: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(raw_line)
    if pending and output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in pending.items())
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_setting(value: Any, name: str, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if "\n" in text or "\r" in text or len(text) > maximum:
        raise ValueError(f"{name} 格式无效")
    return text


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _market_code(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1] if "." in symbol else ""


def _technical_metrics(candles: list[Any]) -> dict[str, float | None]:
    closes = [float(item.close) for item in candles]
    if len(closes) < 20:
        return {}
    ma20 = statistics.fmean(closes[-20:])
    ma60 = statistics.fmean(closes[-60:]) if len(closes) >= 60 else None
    returns = [current / previous - 1 for previous, current in zip(closes, closes[1:]) if previous]
    recent_returns = returns[-20:]
    volatility = (
        statistics.pstdev(recent_returns) * math.sqrt(252) * 100
        if len(recent_returns) >= 2
        else None
    )
    volume_ratio = None
    if len(candles) >= 21:
        average_volume = statistics.fmean(float(item.volume) for item in candles[-21:-1])
        if average_volume > 0:
            volume_ratio = float(candles[-1].volume) / average_volume
    return {
        "ma20": ma20,
        "ma60": ma60,
        "volume_ratio_20d": volume_ratio,
        "annualized_volatility_20d": volatility,
    }


def _decision_signal(
    day_change: float | None,
    relative_change: float | None,
    current_price: float | None,
    metrics: dict[str, float | None],
) -> tuple[str, str, list[str]]:
    score = 0
    reasons: list[str] = []
    if relative_change is not None:
        if relative_change >= 1:
            score += 2
            reasons.append(f"较同市场持仓强 {relative_change:.1f}%")
        elif relative_change <= -1:
            score -= 2
            reasons.append(f"较同市场持仓弱 {abs(relative_change):.1f}%")

    volume_ratio = metrics.get("volume_ratio_20d")
    if volume_ratio is not None and volume_ratio >= 1.5 and day_change is not None:
        if day_change > 0:
            score += 1
            reasons.append(f"放量上涨 {volume_ratio:.1f} 倍")
        elif day_change < 0:
            score -= 1
            reasons.append(f"放量下跌 {volume_ratio:.1f} 倍")

    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    if current_price is not None and ma20:
        if current_price >= ma20:
            score += 1
            reasons.append("现价位于 MA20 上方")
        else:
            score -= 1
            reasons.append("现价跌破 MA20")
    if ma20 is not None and ma60 is not None:
        if ma20 >= ma60:
            score += 1
            reasons.append("中期趋势向上")
        else:
            score -= 1
            reasons.append("中期趋势向下")

    volatility = metrics.get("annualized_volatility_20d")
    if volatility is not None and volatility >= 60:
        score -= 1
        reasons.append(f"波动率较高 {volatility:.0f}%")

    if score >= 3:
        return "add_watch", "加仓观察", reasons[:3]
    if score <= -3:
        return "reduce_watch", "减仓观察", reasons[:3]
    if not reasons:
        reasons.append("暂时没有足够的方向信号")
    return "observe", "保持观察", reasons[:3]


class DashboardApplication:
    def __init__(
        self,
        provider_factory: Callable[[], Any],
        execution_cache_seconds: float = 300,
        analytics_cache_seconds: float = 300,
        news_cache_seconds: float = 600,
        env_path: str | Path = ".env",
    ):
        self._provider_factory = provider_factory
        self._provider: Any | None = None
        self._lock = threading.RLock()
        self._execution_cache_seconds = execution_cache_seconds
        self._execution_cache: dict[str, Any] = {}
        self._execution_cache_symbols: frozenset[str] = frozenset()
        self._execution_cache_at = 0.0
        self._analytics_cache_seconds = analytics_cache_seconds
        self._analytics_cache: dict[str, dict[str, float | None]] = {}
        self._analytics_cache_symbols: frozenset[str] = frozenset()
        self._analytics_cache_at = 0.0
        self._news_cache_seconds = news_cache_seconds
        self._news_cache: dict[str, tuple[float, list[Any]]] = {}
        self._env_path = Path(env_path)
        self._alert_link_signer = AlertLinkSigner(ensure_alert_link_secret(self._env_path))
        self._alert_public_host = _dotenv_values(self._env_path).get(
            "ALERT_PUBLIC_HOST", ""
        ).strip().lower()

    def is_public_alert_host(self, host_header: str) -> bool:
        if not self._alert_public_host:
            return False
        try:
            hostname = urlsplit(f"//{host_header}").hostname or ""
        except ValueError:
            return False
        return hostname.lower() == self._alert_public_host

    def alert_access_allowed(self, query: dict[str, list[str]]) -> bool:
        symbol = query.get("symbol", [""])[0].strip().upper()
        supplied = query.get("token", [""])[0]
        return self._alert_link_signer.verify(supplied, symbol)

    def _get_provider(self):
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    def positions(self, query: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        query = query or {}
        all_sessions = query.get("sessions", ["intraday"])[0] == "all"
        requested_symbol = query.get("symbol", [""])[0].strip().upper()
        if requested_symbol and not SYMBOL_PATTERN.fullmatch(requested_symbol):
            raise ValueError("证券代码格式无效，例如 AAPL.US 或 700.HK")
        with self._lock:
            provider = self._get_provider()
            positions = provider.get_positions()
            if requested_symbol:
                positions = [item for item in positions if item.symbol == requested_symbol]
            symbols = [item.symbol for item in positions]
            quotes = provider.get_quotes(
                (item.symbol for item in positions),
                include_extended_hours=all_sessions,
            )
            references = self._last_executions(provider, symbols)
            analytics = self._technical_analytics(provider, symbols)

        market_changes: dict[str, list[float]] = {}
        currency_values: dict[str, Decimal] = {}
        for item in positions:
            quote = quotes.get(item.symbol)
            if quote and quote.day_change_pct is not None:
                market_changes.setdefault(_market_code(item.symbol), []).append(
                    float(quote.day_change_pct)
                )
            if quote:
                currency_values[item.currency] = currency_values.get(
                    item.currency, Decimal("0")
                ) + abs(item.quantity * quote.last_price)
        market_medians = {
            market: statistics.median(changes)
            for market, changes in market_changes.items()
            if changes
        }

        payload = []
        for item in positions:
            quote = quotes.get(item.symbol)
            reference = references.get(item.symbol)
            execution = reference.last_trade if reference else None
            last_buy = reference.last_buy if reference else None
            price_difference = (
                quote.last_price - execution.price if quote and execution else None
            )
            difference_pct = (
                price_difference / execution.price * 100
                if price_difference is not None and execution.price > 0
                else None
            )
            buy_price_difference = (
                quote.last_price - last_buy.price if quote and last_buy else None
            )
            buy_difference_pct = (
                buy_price_difference / last_buy.price * 100
                if buy_price_difference is not None and last_buy.price > 0
                else None
            )
            technical = analytics.get(item.symbol, {})
            current_price = float(quote.last_price) if quote else None
            day_change = (
                float(quote.day_change_pct)
                if quote and quote.day_change_pct is not None
                else None
            )
            market_median = market_medians.get(_market_code(item.symbol))
            relative_change = (
                day_change - market_median
                if day_change is not None and market_median is not None
                else None
            )
            signal, signal_label, signal_reasons = _decision_signal(
                day_change,
                relative_change,
                current_price,
                technical,
            )
            ma20 = technical.get("ma20")
            ma60 = technical.get("ma60")
            market_value = abs(item.quantity * quote.last_price) if quote else None
            currency_total = currency_values.get(item.currency)
            currency_weight_pct = (
                market_value / currency_total * 100
                if market_value is not None and currency_total
                else None
            )
            payload.append({
                "symbol": item.symbol,
                "name": item.name,
                "quantity": _json_value(item.quantity),
                "cost_price": _json_value(item.cost_price),
                "currency": item.currency,
                "current_price": _json_value(quote.last_price) if quote else None,
                "previous_close": _json_value(quote.previous_close) if quote else None,
                "day_change_pct": _json_value(quote.day_change_pct) if quote else None,
                "quote_timestamp": quote.timestamp if quote else None,
                "session": quote.session if quote else None,
                "last_trade_price": _json_value(execution.price) if execution else None,
                "last_trade_quantity": _json_value(execution.quantity) if execution else None,
                "last_trade_timestamp": execution.timestamp if execution else None,
                "last_trade_side": execution.side if execution else None,
                "price_vs_last_trade": _json_value(price_difference),
                "price_vs_last_trade_pct": _json_value(difference_pct),
                "last_buy_price": _json_value(last_buy.price) if last_buy else None,
                "last_buy_quantity": _json_value(last_buy.quantity) if last_buy else None,
                "last_buy_timestamp": last_buy.timestamp if last_buy else None,
                "price_vs_last_buy": _json_value(buy_price_difference),
                "price_vs_last_buy_pct": _json_value(buy_difference_pct),
                "market_value": _json_value(market_value),
                "currency_weight_pct": _json_value(currency_weight_pct),
                "market_median_change_pct": market_median,
                "relative_change_pct": relative_change,
                "ma20": ma20,
                "ma60": ma60,
                "distance_ma20_pct": (
                    (current_price / ma20 - 1) * 100
                    if current_price is not None and ma20
                    else None
                ),
                "distance_ma60_pct": (
                    (current_price / ma60 - 1) * 100
                    if current_price is not None and ma60
                    else None
                ),
                "volume_ratio_20d": technical.get("volume_ratio_20d"),
                "annualized_volatility_20d": technical.get(
                    "annualized_volatility_20d"
                ),
                "decision_signal": signal,
                "decision_label": signal_label,
                "decision_reasons": signal_reasons,
            })
        return payload

    def _last_executions(self, provider: Any, symbols: list[str]) -> dict[str, Any]:
        symbol_set = frozenset(symbols)
        now = time.monotonic()
        cache_fresh = now - self._execution_cache_at < self._execution_cache_seconds
        if symbol_set == self._execution_cache_symbols and cache_fresh:
            return self._execution_cache
        try:
            executions = provider.get_last_executions(symbols)
        except Exception as exc:
            logger.warning("读取最近成交记录失败: %s", exc)
            return self._execution_cache if symbol_set == self._execution_cache_symbols else {}
        self._execution_cache = executions
        self._execution_cache_symbols = symbol_set
        self._execution_cache_at = now
        return executions

    def _technical_analytics(
        self,
        provider: Any,
        symbols: list[str],
    ) -> dict[str, dict[str, float | None]]:
        symbol_set = frozenset(symbols)
        now = time.monotonic()
        cache_fresh = now - self._analytics_cache_at < self._analytics_cache_seconds
        if symbol_set == self._analytics_cache_symbols and cache_fresh:
            return self._analytics_cache
        analytics: dict[str, dict[str, float | None]] = {}
        for symbol in symbols:
            try:
                candles = provider.get_candlesticks(
                    symbol=symbol,
                    period="day",
                    count=61,
                    forward_adjust=True,
                    all_sessions=False,
                )
                analytics[symbol] = _technical_metrics(candles)
            except Exception as exc:
                logger.warning("读取 %s 风险指标失败: %s", symbol, exc)
        self._analytics_cache = analytics
        self._analytics_cache_symbols = symbol_set
        self._analytics_cache_at = now
        return analytics

    def news(self, query: dict[str, list[str]]) -> dict[str, Any]:
        symbol = query.get("symbol", [""])[0].strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("证券代码格式无效，例如 AAPL.US 或 700.HK")
        try:
            count = int(query.get("count", ["5"])[0])
        except ValueError as exc:
            raise ValueError("新闻数量必须是整数") from exc
        if not 1 <= count <= 10:
            raise ValueError("新闻数量必须在 1 到 10 之间")
        now = time.monotonic()
        with self._lock:
            cached = self._news_cache.get(symbol)
            if cached and now - cached[0] < self._news_cache_seconds:
                articles = cached[1]
            else:
                try:
                    articles = self._get_provider().get_news(symbol, count=10)
                    self._news_cache[symbol] = (now, articles)
                except Exception:
                    if not cached:
                        raise
                    logger.warning("刷新 %s 新闻失败，继续使用缓存", symbol)
                    articles = cached[1]
        return {
            "symbol": symbol,
            "articles": [
                {
                    "id": item.article_id,
                    "title": item.title,
                    "published_at": item.published_at,
                    "url": item.url,
                    "comments_count": item.comments_count,
                    "likes_count": item.likes_count,
                    "category": item.category,
                    "category_label": item.category_label,
                }
                for item in articles[:count]
            ],
        }

    def bark_settings(self) -> dict[str, Any]:
        values = _dotenv_values(self._env_path)
        return {
            "configured": bool(values.get("BARK_DEVICE_KEY", "").strip()),
            "server_url": values.get("BARK_SERVER_URL", "https://api.day.app"),
            "group": values.get("BARK_GROUP", "LongWatch"),
            "level": values.get("BARK_LEVEL", "timeSensitive"),
            "sound": values.get("BARK_SOUND", ""),
            "detail_base_url": values.get("ALERT_DETAIL_BASE_URL", ""),
        }

    def save_bark_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = _dotenv_values(self._env_path)
        device_key = _safe_setting(payload.get("device_key"), "Device Key")
        if not device_key:
            device_key = current.get("BARK_DEVICE_KEY", "").strip()
        if not device_key:
            raise ValueError("请填写 Bark Device Key")
        server_url = _safe_setting(
            payload.get("server_url", current.get("BARK_SERVER_URL", "https://api.day.app")),
            "Bark Server URL",
            512,
        ).rstrip("/")
        parsed = urlsplit(server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Bark Server URL 必须是完整的 http 或 https 地址")
        group = _safe_setting(payload.get("group", current.get("BARK_GROUP", "LongWatch")), "分组", 64)
        level = _safe_setting(
            payload.get("level", current.get("BARK_LEVEL", "timeSensitive")),
            "通知级别",
            32,
        )
        if level not in BARK_LEVELS:
            raise ValueError("通知级别无效")
        sound = _safe_setting(payload.get("sound", current.get("BARK_SOUND", "")), "声音", 64)
        updates = {
            "BARK_DEVICE_KEY": device_key,
            "BARK_SERVER_URL": server_url,
            "BARK_GROUP": group or "LongWatch",
            "BARK_LEVEL": level,
            "BARK_SOUND": sound,
        }
        _write_dotenv_values(self._env_path, updates)
        os.environ.update(updates)
        return self.bark_settings()

    def test_bark(self) -> None:
        values = _dotenv_values(self._env_path)
        device_key = values.get("BARK_DEVICE_KEY", "").strip()
        if not device_key:
            raise ValueError("请先保存 Bark Device Key")
        symbol = "INTC.US"
        with self._lock:
            quote = self._get_provider().get_quotes(
                [symbol], include_extended_hours=True
            ).get(symbol)
        if quote is None:
            raise RuntimeError("未读取到 INTC.US 实时行情")
        day_change = quote.day_change_pct
        day_text = "暂无" if day_change is None else f"{day_change:+.2f}%"
        simulated_execution_price = quote.last_price / Decimal("0.948")
        detail_base_url = values.get("ALERT_DETAIL_BASE_URL", "").strip()
        detail_url = ""
        if detail_base_url:
            parsed = urlsplit(detail_base_url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query.update({
                "symbol": symbol,
                "view": "alert",
                "token": self._alert_link_signer.issue(symbol),
            })
            detail_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), parsed.fragment)
            )
        notifier = BarkNotifier(
            server_url=values.get("BARK_SERVER_URL", "https://api.day.app"),
            device_key=device_key,
            group=values.get("BARK_GROUP", "LongWatch"),
            level=values.get("BARK_LEVEL", "timeSensitive"),
            sound=values.get("BARK_SOUND", ""),
        )
        notifier.send(
            "【测试】INTC 较今日最后成交下跌 -5.20%",
            (
                f"{symbol}｜{quote.session}\n"
                f"实时价格 {quote.last_price} USD\n"
                f"当日涨跌 {day_text}\n"
                f"模拟今日最后成交 {simulated_execution_price:.3f} USD\n"
                "较今日成交 -5.20%\n"
                "这是测试通知，不会记录为真实告警。"
            ),
            detail_url,
        )

    def candlesticks(self, query: dict[str, list[str]]) -> dict[str, Any]:
        symbol = query.get("symbol", [""])[0].strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("证券代码格式无效，例如 AAPL.US 或 700.HK")
        period = query.get("period", ["day"])[0]
        if period not in PERIODS:
            raise ValueError("K 线周期无效")
        try:
            count = int(query.get("count", ["200"])[0])
        except ValueError as exc:
            raise ValueError("K 线数量必须是整数") from exc
        if not 20 <= count <= 1000:
            raise ValueError("K 线数量必须在 20 到 1000 之间")
        adjusted = query.get("adjust", ["forward"])[0] == "forward"
        all_sessions = query.get("sessions", ["intraday"])[0] == "all"
        with self._lock:
            candles = self._get_provider().get_candlesticks(
                symbol=symbol,
                period=period,
                count=count,
                forward_adjust=adjusted,
                all_sessions=all_sessions,
            )
        return {
            "symbol": symbol,
            "period": period,
            "adjust": "forward" if adjusted else "none",
            "candles": [
                {
                    "timestamp": item.timestamp,
                    "open": _json_value(item.open),
                    "high": _json_value(item.high),
                    "low": _json_value(item.low),
                    "close": _json_value(item.close),
                    "volume": item.volume,
                    "turnover": _json_value(item.turnover),
                    "trade_session": item.trade_session,
                }
                for item in candles
            ],
        }


def make_handler(application: DashboardApplication):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "LongWatch/0.1"

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            query = parse_qs(request.query)
            try:
                public_alert_request = application.is_public_alert_host(
                    self.headers.get("Host", "")
                )
                public_alert_api = request.path in {
                    "/api/positions",
                    "/api/candlesticks",
                    "/api/news",
                }
                if public_alert_request and public_alert_api:
                    if not application.alert_access_allowed(query):
                        self._send_json(
                            HTTPStatus.FORBIDDEN, {"error": "告警链接无效或已过期"}
                        )
                        return
                elif public_alert_request and request.path not in {
                    "/healthz",
                    "/",
                    "/index.html",
                    "/app.css",
                    "/app.js",
                }:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "告警域名仅允许访问告警详情"})
                    return
                if request.path == "/healthz":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                elif request.path == "/api/positions":
                    self._send_json(
                        HTTPStatus.OK,
                        {"positions": application.positions(parse_qs(request.query))},
                    )
                elif request.path == "/api/candlesticks":
                    payload = application.candlesticks(parse_qs(request.query))
                    self._send_json(HTTPStatus.OK, payload)
                elif request.path == "/api/news":
                    payload = application.news(parse_qs(request.query))
                    self._send_json(HTTPStatus.OK, payload)
                elif request.path == "/api/settings/bark":
                    self._send_json(HTTPStatus.OK, application.bark_settings())
                elif request.path in STATIC_FILES:
                    is_detail = (
                        bool(query.get("symbol", [""])[0])
                        and query.get("view", [""])[0] != "portfolio"
                    )
                    if (
                        public_alert_request
                        and request.path in {"/", "/index.html"}
                        and not is_detail
                    ) or (is_detail and not application.alert_access_allowed(query)):
                        self._send_alert_forbidden()
                        return
                    self._send_static(*STATIC_FILES[request.path])
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                if request.path.startswith("/api/"):
                    logger.warning("处理 %s 失败: %s", request.path, exc)
                else:
                    logger.exception("处理 %s 失败", request.path)
                hint = "请确认已安装 longbridge SDK，并在 .env 中填写 Longbridge API 凭证。"
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "Longbridge 行情服务暂不可用", "detail": str(exc), "hint": hint},
                )

        def do_POST(self) -> None:
            request = urlsplit(self.path)
            if application.is_public_alert_host(self.headers.get("Host", "")):
                self._send_json(
                    HTTPStatus.FORBIDDEN, {"error": "告警域名不允许修改设置"}
                )
                return
            if request.path not in {"/api/settings/bark", "/api/settings/bark/test"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                return
            if self.headers.get("X-LongWatch-Request") != "settings":
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "设置请求校验失败"})
                return
            try:
                payload = self._read_json()
                if request.path == "/api/settings/bark":
                    self._send_json(HTTPStatus.OK, application.save_bark_settings(payload))
                else:
                    application.test_bark()
                    self._send_json(HTTPStatus.OK, {"ok": True})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                logger.warning("Bark 设置操作失败: %s", exc)
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "Bark 操作失败", "detail": str(exc)},
                )

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("请求长度无效") from exc
            if not 0 <= length <= 16384:
                raise ValueError("设置数据过大")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise ValueError("设置数据不是有效 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("设置数据格式无效")
            return payload

        def _send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, filename: str, content_type: str) -> None:
            body = files("longwatch").joinpath("web_assets", filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _send_alert_forbidden(self) -> None:
            body = (
                "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>告警链接无效</title><body style='margin:0;background:#090b0f;"
                "color:#eef1f4;font-family:-apple-system,sans-serif;display:grid;"
                "min-height:100vh;place-items:center'><main style='padding:28px;"
                "text-align:center'><h1 style='font-size:22px'>告警链接无效</h1>"
                "<p style='color:#818b98'>链接可能已超过 24 小时，请从最新的 Bark 通知重新打开。</p>"
                "</main></body></html>"
            ).encode("utf-8")
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            message = re.sub(
                r"([?&]token=)[^&\s]+", r"\1[REDACTED]", format % args
            )
            logger.info("%s - %s", self.address_string(), message)

    return DashboardHandler


def create_server(
    host: str,
    port: int,
    provider_factory: Callable[[], Any],
    env_path: str | Path = ".env",
) -> ThreadingHTTPServer:
    application = DashboardApplication(provider_factory, env_path=env_path)
    return ThreadingHTTPServer((host, port), make_handler(application))


def run_server(
    host: str,
    port: int,
    provider_factory: Callable[[], Any],
    env_path: str | Path = ".env",
) -> None:
    server = create_server(host, port, provider_factory, env_path=env_path)
    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    logger.info("K 线服务已启动: http://%s:%s", display_host, actual_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在停止 K 线服务")
    finally:
        server.server_close()
