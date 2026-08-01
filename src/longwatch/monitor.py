from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .alert_links import AlertLinkSigner
from .models import Alert, Snapshot

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, title: str, body: str, url: str = "") -> None: ...


@dataclass(frozen=True)
class Rule:
    name: str
    label: str
    direction: str
    threshold: Decimal

    def active(self, value: Decimal) -> bool:
        return value >= self.threshold if self.direction == "up" else value <= -self.threshold

    def recovered(self, value: Decimal, hysteresis: Decimal) -> bool:
        if self.direction == "up":
            return value <= self.threshold - hysteresis
        return value >= -self.threshold + hysteresis


class AlertState:
    def __init__(self, path: Path):
        self.path = path
        self.active: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            active = data.get("active", {})
            if isinstance(active, dict):
                self.active = {str(key): bool(value) for key, value in active.items()}
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("无法读取告警状态 %s: %s", self.path, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps({"active": self.active}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class AlertEngine:
    def __init__(
        self,
        state: AlertState,
        day_rise_pct: float,
        day_fall_pct: float,
        cost_gain_pct: float,
        cost_loss_pct: float,
        hysteresis_pct: float,
        detail_base_url: str = "",
        detail_secret: str = "",
        link_clock=time.time,
    ):
        self.state = state
        self.hysteresis = Decimal(str(hysteresis_pct))
        self.detail_base_url = detail_base_url.strip()
        self._link_signer = (
            AlertLinkSigner(detail_secret.strip(), clock=link_clock) if detail_secret.strip() else None
        )
        self.rules = (
            Rule("day_up", "当日上涨", "up", Decimal(str(day_rise_pct))),
            Rule("day_down", "当日下跌", "down", Decimal(str(day_fall_pct))),
            Rule("cost_up", "持仓盈利", "up", Decimal(str(cost_gain_pct))),
            Rule("cost_down", "持仓亏损", "down", Decimal(str(cost_loss_pct))),
        )

    def evaluate(self, snapshot: Snapshot) -> list[Alert]:
        metrics = {
            "day": snapshot.quote.day_change_pct,
            "cost": snapshot.cost_change_pct,
        }
        alerts: list[Alert] = []
        changed = False
        for rule in self.rules:
            metric = metrics["day" if rule.name.startswith("day_") else "cost"]
            if metric is None:
                continue
            key = f"{snapshot.position.symbol}:{rule.name}"
            was_active = self.state.active.get(key, False)
            if not was_active and rule.active(metric):
                alerts.append(self._build_alert(key, rule, snapshot, metric))
            elif was_active and rule.recovered(metric, self.hysteresis):
                self.state.active[key] = False
                changed = True
        if changed:
            self.state.save()
        return alerts

    def confirm_sent(self, alert: Alert) -> None:
        """Persist only after Bark accepted the push, so a network error cannot lose it."""
        if not self.state.active.get(alert.key, False):
            self.state.active[alert.key] = True
            self.state.save()

    def _build_alert(self, key: str, rule: Rule, snapshot: Snapshot, value: Decimal) -> Alert:
        position = snapshot.position
        quote = snapshot.quote
        sign = "+" if value >= 0 else ""
        title = f"{position.name} {rule.label} {sign}{value:.2f}%"
        day = quote.day_change_pct
        cost = snapshot.cost_change_pct
        day_text = "--" if day is None else f"{day:+.2f}%"
        cost_text = "--" if cost is None else f"{cost:+.2f}%"
        body = (
            f"{position.symbol}｜{quote.session}\n"
            f"现价 {quote.last_price} {position.currency}\n"
            f"当日 {day_text}｜持仓 {cost_text}\n"
            f"数量 {position.quantity}｜未实现盈亏 {snapshot.unrealized_pnl:+.2f} {position.currency}"
        )
        return Alert(
            key=key,
            title=title,
            body=body,
            url=self._detail_url(position.symbol),
        )

    def _detail_url(self, symbol: str) -> str:
        if not self.detail_base_url:
            return ""
        parsed = urlsplit(self.detail_base_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["symbol"] = symbol
        query["view"] = "alert"
        if self._link_signer:
            query["token"] = self._link_signer.issue(symbol)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), parsed.fragment)
        )

    def remove_stale_symbols(self, current_symbols: set[str]) -> None:
        stale = [key for key in self.state.active if key.split(":", 1)[0] not in current_symbols]
        if stale:
            for key in stale:
                del self.state.active[key]
            self.state.save()
