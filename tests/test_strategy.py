import unittest

from strategy import Confirmation, ExitAnalyser, analyse_book


class AnalyseBookTests(unittest.TestCase):
    def test_calculates_bid_share_and_spread(self):
        signal = analyse_book({
            "bids": [["100", "2"]],
            "asks": [["101", "2"]],
        })
        self.assertAlmostEqual(signal.ratio, 50.0)
        self.assertGreater(signal.spread_bps, 0)
        self.assertAlmostEqual(signal.mid, 100.5)

    def test_rejects_empty_or_crossed_book(self):
        with self.assertRaises(ValueError):
            analyse_book({"bids": [], "asks": [["101", "1"]]})
        with self.assertRaises(ValueError):
            analyse_book({"bids": [["102", "1"]], "asks": [["101", "1"]]})

    def test_rejects_non_finite_and_non_positive_levels(self):
        with self.assertRaises(ValueError):
            analyse_book({"bids": [["100", "NaN"]], "asks": [["101", "1"]]})
        with self.assertRaises(ValueError):
            analyse_book({"bids": [["100", "-1"]], "asks": [["101", "1"]]})


class ConfirmationTests(unittest.TestCase):
    def test_requires_continuous_confirmation(self):
        gate = Confirmation(seconds=10)
        self.assertFalse(gate.update("BTCUSDT", 91, 90, now=100))
        self.assertTrue(gate.update("BTCUSDT", 91, 90, now=110))

    def test_gap_longer_than_15_seconds_restarts_confirmation(self):
        gate = Confirmation(seconds=10)
        self.assertFalse(gate.update("BTCUSDT", 91, 90, now=100))
        self.assertFalse(gate.update("BTCUSDT", 91, 90, now=116))
        self.assertFalse(gate.update("BTCUSDT", 91, 90, now=125))

    def test_threshold_or_explicit_reset_breaks_confirmation(self):
        gate = Confirmation(seconds=10)
        gate.update("BTCUSDT", 91, 90, now=100)
        self.assertFalse(gate.update("BTCUSDT", 90, 90, now=105))
        self.assertFalse(gate.update("BTCUSDT", 91, 90, now=106))


class ExitAnalyserTests(unittest.TestCase):
    def test_defensive_exit_requires_sustained_pressure_and_loss(self):
        exits = ExitAnalyser(bearish_seconds=10)
        self.assertEqual(
            exits.evaluate("BTCUSDT", 8, 100, -0.5, now=100),
            (False, "below_minimum_profit"),
        )
        self.assertEqual(
            exits.evaluate("BTCUSDT", 8, 99, -0.5, now=105),
            (False, "below_minimum_profit"),
        )
        self.assertEqual(
            exits.evaluate("BTCUSDT", 8, 98, -0.5, now=110),
            (True, "sustained_bearish_pressure"),
        )

    def test_single_bearish_observation_does_not_trigger(self):
        exits = ExitAnalyser(bearish_seconds=10)
        exits.evaluate("BTCUSDT", 8, 100, -0.5, now=100)
        self.assertEqual(
            exits.evaluate("BTCUSDT", 60, 101, -0.5, now=111),
            (False, "below_minimum_profit"),
        )

    def test_upper_profit_limit_is_preserved(self):
        exits = ExitAnalyser()
        self.assertEqual(
            exits.evaluate("BTCUSDT", 60, 100, 40, now=100),
            (True, "upper_profit_limit"),
        )


if __name__ == "__main__":
    unittest.main()
