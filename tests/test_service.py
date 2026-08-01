from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from longwatch.alert_links import AlertLinkSigner
from longwatch.models import Position, Quote
from longwatch.monitor import AlertEngine, AlertState
from longwatch.service import MonitorService


class FakeProvider:
    def __init__(self):
        self.position_calls = 0

    def get_positions(self):
        self.position_calls += 1
        return [Position("AAPL.US", "Apple", Decimal("1"), Decimal("100"), "USD")]

    def get_quotes(self, symbols):
        return {"AAPL.US": Quote("AAPL.US", Decimal("104"), Decimal("100"), 1)}


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def send(self, title, body, url=""):
        if self.fail:
            raise RuntimeError("offline")
        self.messages.append((title, body, url))


class ServiceTests(TestCase):
    def test_successful_notification_is_confirmed(self):
        with TemporaryDirectory() as directory:
            engine = AlertEngine(
                AlertState(Path(directory) / "state.json"),
                3, 3, 10, 5, 0.3,
                detail_base_url="http://127.0.0.1:8765",
                detail_secret="test-signing-secret",
            )
            notifier = FakeNotifier()
            service = MonitorService(FakeProvider(), notifier, engine, 30, 300, clock=lambda: 0)
            snapshots = service.check_once()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(len(notifier.messages), 1)
            parsed = urlsplit(notifier.messages[0][2])
            query = parse_qs(parsed.query)
            self.assertEqual(query["symbol"], ["AAPL.US"])
            self.assertEqual(query["view"], ["alert"])
            self.assertTrue(
                AlertLinkSigner("test-signing-secret").verify(
                    query["token"][0], "AAPL.US"
                )
            )
            self.assertTrue(engine.state.active["AAPL.US:day_up"])

    def test_failed_notification_is_not_confirmed(self):
        with TemporaryDirectory() as directory:
            engine = AlertEngine(AlertState(Path(directory) / "state.json"), 3, 3, 10, 5, 0.3)
            service = MonitorService(FakeProvider(), FakeNotifier(fail=True), engine, 30, 300, clock=lambda: 0)
            with self.assertRaises(RuntimeError):
                service.check_once()
            self.assertFalse(engine.state.active.get("AAPL.US:day_up", False))
