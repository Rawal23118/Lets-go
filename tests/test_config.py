import math
import unittest
from dataclasses import replace

from config import Config


class ConfigValidationTests(unittest.TestCase):
    def test_paper_defaults_and_allocation_hard_cap(self):
        cfg = Config()
        self.assertEqual(cfg.mode, "paper")
        self.assertEqual(cfg.allocation, 20)
        cfg.validate()
        with self.assertRaises(ValueError):
            replace(cfg, allocation=20.01).validate()

    def test_rejects_non_finite_strategy_values(self):
        cfg = Config()
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    replace(cfg, allocation=value).validate()
                with self.assertRaises(ValueError):
                    replace(cfg, max_spread_bps=value).validate()

    def test_rejects_invalid_scan_and_volume_settings(self):
        cfg = Config()
        with self.assertRaises(ValueError):
            replace(cfg, scan_interval=0).validate()
        with self.assertRaises(ValueError):
            replace(cfg, min_quote_volume=0).validate()


if __name__ == "__main__":
    unittest.main()
