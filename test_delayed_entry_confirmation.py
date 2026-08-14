import unittest

import pandas as pd

from delayed_entry_confirmation import assess_delayed_entry


class DelayedEntryConfirmationTests(unittest.TestCase):
    @staticmethod
    def candles(values):
        return pd.DataFrame({"close": values})

    def test_accepts_buy_when_uptrend_remains_intact(self):
        result = assess_delayed_entry("BUY", self.candles(range(100, 130)), 131.0)
        self.assertTrue(result.accepted)
        self.assertEqual(result.projected_trend, "UP")

    def test_rejects_buy_when_refreshed_move_is_adverse(self):
        result = assess_delayed_entry("BUY", self.candles(range(100, 130)), 100.0)
        self.assertFalse(result.accepted)

    def test_accepts_sell_when_downtrend_remains_intact(self):
        result = assess_delayed_entry("SELL", self.candles(range(130, 100, -1)), 99.0)
        self.assertTrue(result.accepted)
        self.assertEqual(result.projected_trend, "DOWN")

    def test_rejects_sell_when_refreshed_move_is_adverse(self):
        result = assess_delayed_entry("SELL", self.candles(range(130, 100, -1)), 135.0)
        self.assertFalse(result.accepted)

    def test_fails_closed_when_quote_is_missing(self):
        result = assess_delayed_entry("BUY", self.candles(range(100, 130)), None)
        self.assertFalse(result.accepted)
        self.assertIn("unavailable", result.reason)

    def test_fails_closed_when_history_is_short(self):
        result = assess_delayed_entry("BUY", self.candles(range(10)), 20.0)
        self.assertFalse(result.accepted)
        self.assertIn("21", result.reason)


if __name__ == "__main__":
    unittest.main()
