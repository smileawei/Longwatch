from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from longwatch.alert_links import AlertLinkSigner
from longwatch.models import LastExecution, Position, Quote, Snapshot
from longwatch.monitor import AlertEngine, AlertState


class AlertEngineTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "state.json"
        self.state = AlertState(self.state_path)
        self.engine = AlertEngine(self.state, 3, 3, 10, 5, 0.3)
        self.position = Position("AAPL.US", "Apple", Decimal("2"), Decimal("100"), "USD")

    def tearDown(self):
        self.tempdir.cleanup()

    def snapshot(
        self,
        price: str,
        previous_close: str = "100",
        execution_price: str | None = None,
        execution_side: str = "买入",
    ) -> Snapshot:
        execution = None
        if execution_price is not None:
            execution = LastExecution(
                "AAPL.US", Decimal(execution_price), Decimal("1"), 1, execution_side
            )
        return Snapshot(
            self.position,
            Quote("AAPL.US", Decimal(price), Decimal(previous_close), 1),
            execution,
        )

    def test_alert_is_deduplicated_after_success(self):
        alerts = self.engine.evaluate(self.snapshot("104"))
        self.assertEqual([alert.key for alert in alerts], ["AAPL.US:day_up"])
        self.engine.confirm_sent(alerts[0])
        self.assertEqual(self.engine.evaluate(self.snapshot("105")), [])

        reloaded = AlertEngine(AlertState(self.state_path), 3, 3, 10, 5, 0.3)
        self.assertEqual(reloaded.evaluate(self.snapshot("105")), [])

    def test_unconfirmed_alert_is_retried(self):
        first = self.engine.evaluate(self.snapshot("104"))
        second = self.engine.evaluate(self.snapshot("104"))
        self.assertEqual(first[0].key, second[0].key)

    def test_recovery_rearms_rule(self):
        alert = self.engine.evaluate(self.snapshot("104"))[0]
        self.engine.confirm_sent(alert)
        self.assertEqual(self.engine.evaluate(self.snapshot("102.6")), [])
        self.assertFalse(self.state.active["AAPL.US:day_up"])
        self.assertEqual(len(self.engine.evaluate(self.snapshot("104"))), 1)

    def test_execution_loss_alert_body(self):
        alerts = self.engine.evaluate(
            self.snapshot("94", previous_close="95", execution_price="100")
        )
        execution_alert = next(
            alert for alert in alerts if alert.key.endswith("execution_down")
        )
        self.assertIn("较今日最后成交下跌 -6.00%", execution_alert.title)
        self.assertIn("今日最后成交 买入 100 USD", execution_alert.body)
        self.assertIn("未实现盈亏 -12.00 USD", execution_alert.body)

    def test_execution_gain_uses_last_trade_instead_of_position_cost(self):
        alerts = self.engine.evaluate(
            self.snapshot("111", previous_close="110", execution_price="100", execution_side="卖出")
        )
        self.assertEqual([alert.key for alert in alerts], ["AAPL.US:execution_up"])
        self.assertIn("今日最后成交 卖出 100 USD", alerts[0].body)

    def test_execution_rules_are_skipped_without_a_trade_today(self):
        alerts = self.engine.evaluate(self.snapshot("111", previous_close="110"))
        self.assertEqual(alerts, [])

    def test_new_execution_rearms_rules_immediately(self):
        first_snapshot = self.snapshot(
            "111", previous_close="110", execution_price="100"
        )
        first_alert = self.engine.evaluate(first_snapshot)[0]
        self.engine.confirm_sent(first_alert)
        self.assertEqual(self.engine.evaluate(first_snapshot), [])

        second_snapshot = Snapshot(
            self.position,
            Quote("AAPL.US", Decimal("111"), Decimal("110"), 2),
            LastExecution(
                "AAPL.US", Decimal("100"), Decimal("2"), 2, "卖出"
            ),
        )
        alerts = self.engine.evaluate(second_snapshot)
        self.assertEqual([alert.key for alert in alerts], ["AAPL.US:execution_up"])

    def test_no_trade_today_clears_previous_execution_reference(self):
        snapshot = self.snapshot("111", previous_close="110", execution_price="100")
        alert = self.engine.evaluate(snapshot)[0]
        self.engine.confirm_sent(alert)
        self.assertIn("AAPL.US", self.state.execution_references)

        self.engine.evaluate(self.snapshot("111", previous_close="110"))
        self.assertNotIn("AAPL.US", self.state.execution_references)
        self.assertNotIn("AAPL.US:execution_up", self.state.active)

    def test_execution_api_failure_does_not_clear_alert_state(self):
        snapshot = self.snapshot("111", previous_close="110", execution_price="100")
        alert = self.engine.evaluate(snapshot)[0]
        self.engine.confirm_sent(alert)

        unavailable_snapshot = Snapshot(
            self.position,
            Quote("AAPL.US", Decimal("111"), Decimal("110"), 2),
            execution_data_available=False,
        )
        self.assertEqual(self.engine.evaluate(unavailable_snapshot), [])
        self.assertIn("AAPL.US", self.state.execution_references)
        self.assertTrue(self.state.active["AAPL.US:execution_up"])

    def test_legacy_cost_rule_state_is_removed(self):
        self.state.active["AAPL.US:cost_down"] = True
        self.state.active["MSFT.US:day_up"] = True
        self.engine.remove_stale_symbols({"AAPL.US"})
        self.assertNotIn("AAPL.US:cost_down", self.state.active)
        self.assertNotIn("MSFT.US:day_up", self.state.active)

    def test_alert_contains_symbol_detail_url(self):
        engine = AlertEngine(
            self.state,
            3,
            3,
            10,
            5,
            0.3,
            detail_base_url="https://watch.example/dashboard?source=bark",
            detail_secret="test-signing-secret",
        )
        alert = engine.evaluate(self.snapshot("104"))[0]
        parsed = urlsplit(alert.url)
        query = parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://watch.example/dashboard")
        self.assertEqual(query["source"], ["bark"])
        self.assertEqual(query["symbol"], ["AAPL.US"])
        self.assertEqual(query["view"], ["alert"])
        self.assertTrue(
            AlertLinkSigner("test-signing-secret").verify(query["token"][0], "AAPL.US")
        )
