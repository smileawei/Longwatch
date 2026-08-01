from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    quantity: Decimal
    cost_price: Decimal
    currency: str


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: Decimal
    previous_close: Decimal
    timestamp: int
    session: str = "常规"

    @property
    def day_change_pct(self) -> Decimal | None:
        if self.previous_close <= 0:
            return None
        return (self.last_price / self.previous_close - 1) * 100


@dataclass(frozen=True)
class LastExecution:
    symbol: str
    price: Decimal
    quantity: Decimal
    timestamp: int
    side: str = ""


@dataclass(frozen=True)
class ExecutionReferences:
    last_trade: LastExecution | None = None
    last_buy: LastExecution | None = None


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    title: str
    published_at: str
    url: str
    comments_count: int = 0
    likes_count: int = 0
    category: str = "other"
    category_label: str = "其他"


@dataclass(frozen=True)
class Snapshot:
    position: Position
    quote: Quote

    @property
    def cost_change_pct(self) -> Decimal | None:
        cost_value = abs(self.position.cost_price * self.position.quantity)
        if cost_value <= 0:
            return None
        return self.unrealized_pnl / cost_value * 100

    @property
    def market_value(self) -> Decimal:
        return self.quote.last_price * self.position.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.quote.last_price - self.position.cost_price) * self.position.quantity


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    body: str
    url: str = ""


@dataclass(frozen=True)
class Candlestick:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal
    trade_session: str = ""
