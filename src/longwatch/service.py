from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .models import Position, Snapshot
from .monitor import AlertEngine, Notifier

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        provider,
        notifier: Notifier,
        engine: AlertEngine,
        poll_interval_seconds: float,
        positions_refresh_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.provider = provider
        self.notifier = notifier
        self.engine = engine
        self.poll_interval_seconds = poll_interval_seconds
        self.positions_refresh_seconds = positions_refresh_seconds
        self.clock = clock
        self.positions: list[Position] = []
        self._positions_loaded_at = float("-inf")

    def refresh_positions(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._positions_loaded_at < self.positions_refresh_seconds:
            return
        self.positions = self.provider.get_positions()
        self._positions_loaded_at = now
        symbols = {position.symbol for position in self.positions}
        self.engine.remove_stale_symbols(symbols)
        logger.info("已刷新持仓，共 %d 个证券: %s", len(symbols), ", ".join(sorted(symbols)) or "无")

    def check_once(self, force_positions_refresh: bool = False) -> list[Snapshot]:
        self.refresh_positions(force=force_positions_refresh)
        if not self.positions:
            return []
        symbols = [position.symbol for position in self.positions]
        quotes = self.provider.get_quotes(symbols)
        execution_data_available = True
        try:
            executions = self.provider.get_today_last_executions(symbols)
        except Exception as exc:
            logger.warning("未能读取当天成交记录，将跳过成交价告警: %s", exc)
            executions = {}
            execution_data_available = False
        snapshots: list[Snapshot] = []
        for position in self.positions:
            quote = quotes.get(position.symbol)
            if quote is None:
                logger.warning("未获得 %s 的行情", position.symbol)
                continue
            snapshot = Snapshot(
                position=position,
                quote=quote,
                last_execution=executions.get(position.symbol),
                execution_data_available=execution_data_available,
            )
            snapshots.append(snapshot)
            logger.debug(
                "%s 现价=%s 当日=%s 较今日成交=%s",
                position.symbol,
                quote.last_price,
                quote.day_change_pct,
                snapshot.execution_change_pct,
            )
            for alert in self.engine.evaluate(snapshot):
                self.notifier.send(alert.title, alert.body, alert.url)
                self.engine.confirm_sent(alert)
                logger.info("已发送告警: %s", alert.title)
        return snapshots

    def run_forever(self) -> None:
        backoff = self.poll_interval_seconds
        while True:
            started = self.clock()
            try:
                self.check_once()
                backoff = self.poll_interval_seconds
            except KeyboardInterrupt:
                logger.info("收到停止信号")
                return
            except Exception:
                logger.exception("本轮监控失败，将在 %.0f 秒后重试", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            elapsed = self.clock() - started
            time.sleep(max(0, self.poll_interval_seconds - elapsed))
