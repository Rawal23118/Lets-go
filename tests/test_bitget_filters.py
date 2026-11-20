import unittest

from bitget import eligible_usdt_crypto_symbols, select_volume_tier_symbols


class InstrumentFilterTests(unittest.TestCase):
    def test_only_verified_ordinary_online_usdt_spot_symbols_are_eligible(self):
        instruments = [
            {"symbol": "BTCUSDT", "category": "SPOT", "quoteCoin": "USDT",
             "status": "online", "isReality": "no", "symbolType": "crypto"},
            {"symbol": "STOCKUSDT", "category": "SPOT", "quoteCoin": "USDT",
             "status": "online", "isReality": "yes", "symbolType": "stock"},
            {"symbol": "ETHBTC", "category": "SPOT", "quoteCoin": "BTC",
             "status": "online", "isReality": "no", "symbolType": "crypto"},
            {"symbol": "OFFUSDT", "category": "SPOT", "quoteCoin": "USDT",
             "status": "offline", "isReality": "no", "symbolType": "crypto"},
        ]
        self.assertEqual(eligible_usdt_crypto_symbols(instruments), {"BTCUSDT"})

    def test_empty_instrument_response_fails_closed(self):
        with self.assertRaises(ValueError):
            eligible_usdt_crypto_symbols([])


class VolumeSelectionTests(unittest.TestCase):
    def test_filters_and_selects_only_eligible_symbols(self):
        tickers = [
            {"symbol": "BTCUSDT", "usdtVolume": "100000"},
            {"symbol": "ETHUSDT", "usdtVolume": "50000"},
            {"symbol": "BADUSDT", "usdtVolume": "900000"},
            {"symbol": "LOWUSDT", "usdtVolume": "1"},
        ]
        selected = select_volume_tier_symbols(
            tickers, {"BTCUSDT", "ETHUSDT"}, 100, 10
        )
        self.assertEqual(set(selected), {"BTCUSDT", "ETHUSDT"})

    def test_empty_selection_is_empty(self):
        self.assertEqual(
            select_volume_tier_symbols([], {"BTCUSDT"}, 100, 10), []
        )


if __name__ == "__main__":
    unittest.main()
