from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import subprocess
from typing import Any

from .models import (
    Candlestick,
    ExecutionReferences,
    LastExecution,
    NewsArticle,
    Position,
    Quote,
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def _news_category(title: str) -> tuple[str, str]:
    lowered = title.lower()
    groups = (
        ("catalyst", "业绩", ("财报", "业绩", "营收", "利润", "eps", "earnings", "revenue", "guidance")),
        ("regulatory", "监管", ("监管", "调查", "处罚", "罚款", "诉讼", "sec", "lawsuit", "fine")),
        ("strategic", "业务", ("收购", "合作", "发布", "新品", "acquisition", "partnership", "launch")),
        ("financial", "资本", ("回购", "分红", "拆股", "增发", "buyback", "dividend", "split", "offering")),
        ("opinion", "评级", ("评级", "目标价", "分析师", "upgrade", "downgrade", "analyst", "price target")),
    )
    for category, label, keywords in groups:
        if any(keyword in lowered for keyword in keywords):
            return category, label
    return "other", "其他"


class LongbridgeProvider:
    """Thin adapter around the official Longbridge Python SDK."""

    def __init__(
        self,
        include_extended_hours: bool = False,
        batch_size: int = 50,
        oauth_client_id: str = "",
        oauth_callback_port: int = 60355,
    ):
        if not oauth_client_id.strip():
            raise ValueError("缺少 LONGBRIDGE_OAUTH_CLIENT_ID；LongWatch 仅支持 OAuth 认证")
        try:
            from longbridge.openapi import Config, OAuthBuilder, QuoteContext, TradeContext
        except ImportError as exc:
            raise RuntimeError("未安装 longbridge SDK，请先执行 pip install -e .") from exc

        oauth = OAuthBuilder(oauth_client_id, oauth_callback_port).build(
            lambda url: print(f"请先完成 Longbridge OAuth 授权: {url}", flush=True)
        )
        config = Config.from_oauth(oauth)
        self._trade = TradeContext(config)
        self._quote = QuoteContext(config)
        self._include_extended_hours = include_extended_hours
        self._batch_size = batch_size

    def get_positions(self) -> list[Position]:
        response = self._trade.stock_positions()
        raw_positions = [position for channel in response.channels for position in channel.positions]

        # 同一证券可能分布在多个账户通道；合并数量并以成本金额加权。
        merged: dict[str, dict[str, Any]] = {}
        for item in raw_positions:
            quantity = _decimal(item.quantity)
            if quantity == 0:
                continue
            current = merged.setdefault(
                item.symbol,
                {
                    "name": item.symbol_name,
                    "quantity": Decimal("0"),
                    "cost_amount": Decimal("0"),
                    "currency": item.currency,
                },
            )
            current["quantity"] += quantity
            current["cost_amount"] += quantity * _decimal(item.cost_price)

        positions: list[Position] = []
        for symbol, item in merged.items():
            quantity = item["quantity"]
            if quantity == 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    name=item["name"],
                    quantity=quantity,
                    cost_price=item["cost_amount"] / quantity,
                    currency=item["currency"],
                )
            )
        return sorted(positions, key=lambda position: position.symbol)

    def get_quotes(
        self,
        symbols: Iterable[str],
        include_extended_hours: bool | None = None,
    ) -> dict[str, Quote]:
        symbols = list(symbols)
        result: dict[str, Quote] = {}
        for index in range(0, len(symbols), self._batch_size):
            for raw_quote in self._quote.quote(symbols[index : index + self._batch_size]):
                result[raw_quote.symbol] = self._convert_quote(raw_quote, include_extended_hours)
        return result

    def get_last_executions(
        self,
        symbols: Iterable[str],
        lookback_days: int = 365,
    ) -> dict[str, ExecutionReferences]:
        """Return the newest completed execution and newest buy for each symbol.

        Longbridge limits an execution-history query to a 90-day window, so older
        windows are read only while a held symbol is still unresolved.
        """
        wanted = set(symbols)
        if not wanted:
            return {}

        newest: dict[str, LastExecution] = {}
        newest_buy: dict[str, LastExecution] = {}
        order_sides: dict[str, str] = {}

        def collect_orders(orders: Iterable[Any]) -> None:
            for order in orders:
                order_sides[str(order.order_id)] = self._order_side_name(order.side)

        def collect_executions(executions: Iterable[Any]) -> None:
            for execution in executions:
                if execution.symbol not in wanted:
                    continue
                order_id = str(execution.order_id)
                side = order_sides.get(order_id, "")
                if not side:
                    try:
                        detail = self._trade.order_detail(order_id)
                        side = self._order_side_name(getattr(detail, "side", ""))
                    except Exception:
                        side = ""
                    order_sides[order_id] = side
                item = LastExecution(
                    symbol=execution.symbol,
                    price=_decimal(execution.price),
                    quantity=_decimal(execution.quantity),
                    timestamp=_timestamp(execution.trade_done_at),
                    side=side,
                )
                current = newest.get(item.symbol)
                if current is None or item.timestamp > current.timestamp:
                    newest[item.symbol] = item
                if side == "买入":
                    current_buy = newest_buy.get(item.symbol)
                    if current_buy is None or item.timestamp > current_buy.timestamp:
                        newest_buy[item.symbol] = item

        collect_orders(self._trade.today_orders())
        collect_executions(self._trade.today_executions())
        unresolved = wanted - (newest.keys() & newest_buy.keys())
        end_at = datetime.now(timezone.utc)
        searched_days = 0
        while unresolved and searched_days < lookback_days:
            window_days = min(90, lookback_days - searched_days)
            start_at = end_at - timedelta(days=window_days)
            collect_orders(self._trade.history_orders(start_at=start_at, end_at=end_at))
            collect_executions(
                self._trade.history_executions(start_at=start_at, end_at=end_at)
            )
            unresolved = wanted - (newest.keys() & newest_buy.keys())
            end_at = start_at
            searched_days += window_days

        return {
            symbol: ExecutionReferences(
                last_trade=newest.get(symbol),
                last_buy=newest_buy.get(symbol),
            )
            for symbol in wanted
            if symbol in newest or symbol in newest_buy
        }

    def get_news(self, symbol: str, count: int = 10) -> list[NewsArticle]:
        """Read public stock news through the Longbridge CLI JSON interface."""
        try:
            completed = subprocess.run(
                [
                    "longbridge",
                    "news",
                    symbol,
                    "--count",
                    str(count),
                    "--format",
                    "json",
                    "--lang",
                    "zh-CN",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("未找到 Longbridge CLI，暂时无法读取股票新闻") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Longbridge 新闻接口响应超时") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "Longbridge 新闻接口调用失败"
            raise RuntimeError(detail)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Longbridge 新闻接口返回了无效数据") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Longbridge 新闻接口返回格式不正确")
        articles: list[NewsArticle] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            url = str(item.get("url", ""))
            if url and not url.startswith(("https://", "http://")):
                url = ""
            category, category_label = _news_category(str(item["title"]))
            articles.append(
                NewsArticle(
                    article_id=str(item.get("id", "")),
                    title=str(item["title"]),
                    published_at=str(item.get("published_at", "")),
                    url=url,
                    comments_count=int(item.get("comments_count", 0) or 0),
                    likes_count=int(item.get("likes_count", 0) or 0),
                    category=category,
                    category_label=category_label,
                )
            )
        return articles

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
        forward_adjust: bool = True,
        all_sessions: bool = False,
    ) -> list[Candlestick]:
        from longbridge.openapi import AdjustType, Period, TradeSessions

        periods = {
            "1m": Period.Min_1,
            "5m": Period.Min_5,
            "15m": Period.Min_15,
            "30m": Period.Min_30,
            "60m": Period.Min_60,
            "day": Period.Day,
            "week": Period.Week,
            "month": Period.Month,
        }
        if period not in periods:
            raise ValueError(f"不支持的 K 线周期: {period}")
        adjust_type = AdjustType.ForwardAdjust if forward_adjust else AdjustType.NoAdjust
        trade_sessions = TradeSessions.All if all_sessions else TradeSessions.Intraday
        response = self._quote.candlesticks(
            symbol,
            periods[period],
            count,
            adjust_type,
            trade_sessions,
        )
        raw_candles = getattr(response, "candlesticks", response)
        candles = [
            Candlestick(
                timestamp=_timestamp(item.timestamp),
                open=_decimal(item.open),
                high=_decimal(item.high),
                low=_decimal(item.low),
                close=_decimal(item.close),
                volume=int(item.volume),
                turnover=_decimal(item.turnover),
                trade_session=self._trade_session_name(getattr(item, "trade_session", "")),
            )
            for item in raw_candles
        ]
        return sorted(candles, key=lambda candle: candle.timestamp)

    def _convert_quote(
        self,
        raw: Any,
        include_extended_hours: bool | None = None,
    ) -> Quote:
        selected = (
            _timestamp(raw.timestamp),
            _decimal(raw.last_done),
            _decimal(raw.prev_close),
            "常规",
        )
        use_extended = (
            self._include_extended_hours
            if include_extended_hours is None
            else include_extended_hours
        )
        if use_extended:
            for attribute, session in (
                ("pre_market_quote", "盘前"),
                ("post_market_quote", "盘后"),
                ("over_night_quote", "夜盘"),
            ):
                extended = getattr(raw, attribute, None)
                if extended is None or not getattr(extended, "last_done", None):
                    continue
                candidate = (
                    _timestamp(extended.timestamp),
                    _decimal(extended.last_done),
                    _decimal(extended.prev_close),
                    session,
                )
                if candidate[0] > selected[0]:
                    selected = candidate
        return Quote(
            symbol=raw.symbol,
            timestamp=selected[0],
            last_price=selected[1],
            previous_close=selected[2],
            session=selected[3],
        )

    @staticmethod
    def _trade_session_name(value: Any) -> str:
        name = str(value).rsplit(".", 1)[-1]
        return {
            "Intraday": "常规",
            "Pre": "盘前",
            "Post": "盘后",
            "Overnight": "夜盘",
        }.get(name, name)

    @staticmethod
    def _order_side_name(value: Any) -> str:
        name = str(value).rsplit(".", 1)[-1]
        return {"Buy": "买入", "Sell": "卖出"}.get(name, name)
