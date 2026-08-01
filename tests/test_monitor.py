from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from longwatch.alert_links import AlertLinkSigner
from longwatch.models import Position, Quote, Snapshot
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

    def snapshot(self, price: str, previous_close: str = "100") -> Snapshot:
        return Snapshot(
            self.position,
            Quote("AAPL.US", Decimal(price), Decimal(previous_close), 1),
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

    def test_cost_loss_alert_body(self):
        alerts = self.engine.evaluate(self.snapshot("94", previous_close="95"))
        cost_alert = next(alert for alert in alerts if alert.key.endswith("cost_down"))
        self.assertIn("持仓亏损 -6.00%", cost_alert.title)
        self.assertIn("未实现盈亏 -12.00 USD", cost_alert.body)

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
