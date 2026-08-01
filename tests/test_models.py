from decimal import Decimal
from unittest import TestCase

from longwatch.models import Position, Quote, Snapshot


class SnapshotTests(TestCase):
    def test_long_position_changes(self):
        snapshot = Snapshot(
            Position("AAPL.US", "Apple", Decimal("2"), Decimal("100"), "USD"),
            Quote("AAPL.US", Decimal("110"), Decimal("105"), 1),
        )
        self.assertEqual(snapshot.quote.day_change_pct.quantize(Decimal("0.01")), Decimal("4.76"))
        self.assertEqual(snapshot.cost_change_pct, Decimal("10.0"))
        self.assertEqual(snapshot.unrealized_pnl, Decimal("20"))

    def test_short_position_pnl_percentage_has_correct_direction(self):
        snapshot = Snapshot(
            Position("AAPL.US", "Apple", Decimal("-2"), Decimal("100"), "USD"),
            Quote("AAPL.US", Decimal("90"), Decimal("95"), 1),
        )
        self.assertEqual(snapshot.unrealized_pnl, Decimal("20"))
        self.assertEqual(snapshot.cost_change_pct, Decimal("10.0"))
