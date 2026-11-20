import sqlite3
import unittest
from decimal import Decimal

from execution_safety import (
    OrderJournal,
    SafetyError,
    floor_to_precision,
    prepare_market_sell,
    validate_market_buy,
)


class PrecisionTests(unittest.TestCase):
    def test_rounds_down_without_exceeding_precision(self):
        self.assertEqual(floor_to_precision("1.239", 2), Decimal("1.23"))

    def test_rejects_invalid_precision_and_non_finite_values(self):
        with self.assertRaises(SafetyError):
            floor_to_precision("1", 19)
        with self.assertRaises(SafetyError):
            floor_to_precision("NaN", 2)


class MarketValidationTests(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "status": "online",
            "minTradeUSDT": "5",
            "maxMarketOrderValue": "100",
            "quantityPrecision": 3,
        }

    def test_market_buy_limits(self):
        self.assertEqual(validate_market_buy(self.meta, "10"), Decimal("10"))
        with self.assertRaises(SafetyError):
            validate_market_buy(self.meta, "4")
        with self.assertRaises(SafetyError):
            validate_market_buy(self.meta, "101")

    def test_market_sell_rounds_down_and_checks_minimum(self):
        qty = prepare_market_sell(self.meta, "0.1239", "100")
        self.assertEqual(qty, Decimal("0.123"))
        with self.assertRaises(SafetyError):
            prepare_market_sell(self.meta, "0.001", "100")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.journal = OrderJournal(self.db)

    def tearDown(self):
        self.db.close()

    def test_persists_intent_and_blocks_duplicate_client_oid(self):
        self.journal.create("client-1", "BTCUSDT", "buy", "10")
        self.assertEqual(self.journal.get("client-1")[5], "prepared")
        self.assertEqual(len(self.journal.unresolved()), 1)
        with self.assertRaises(SafetyError):
            self.journal.create("client-1", "BTCUSDT", "buy", "10")

    def test_terminal_reconciliation_removes_intent_from_unresolved(self):
        self.journal.create("client-2", "BTCUSDT", "sell", "0.1")
        self.journal.update("client-2", "reconciled")
        self.assertEqual(self.journal.unresolved(), [])


if __name__ == "__main__":
    unittest.main()
