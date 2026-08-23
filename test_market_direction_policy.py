import unittest
from types import SimpleNamespace

import pandas as pd

from market_direction_policy import resolve_market_direction
from risk_manager import RiskManager
from strategy import Signal, rebuild_signal_for_direction


CASES = [
    ("BEARISH", "BUY", "NORMAL", "BUY"),
    ("BEARISH", "SELL", "REVERSE", "BUY"),
    ("BULLISH", "BUY", "NORMAL", "BUY"),
    ("BULLISH", "SELL", "NORMAL", "SELL"),
    ("SIDEWAYS", "BUY", "REVERSE", "SELL"),
    ("SIDEWAYS", "SELL", "NORMAL", "SELL"),
]


class MarketDirectionPolicyTests(unittest.TestCase):
    def test_frozen_six_cases(self):
        for market, raw, decision, final in CASES:
            with self.subTest(market=market, raw=raw):
                result = resolve_market_direction(market, raw)
                self.assertEqual(result["decision"], decision)
                self.assertEqual(result["direction"], final)
                self.assertEqual(result["original_direction"], raw)

    def test_aliases_and_unknown_fail_safe(self):
        self.assertEqual(resolve_market_direction("UP", "SELL")["direction"], "SELL")
        self.assertEqual(resolve_market_direction("FLAT", "BUY")["direction"], "SELL")
        self.assertEqual(resolve_market_direction("UNKNOWN", "BUY")["decision"], "SKIP")
        self.assertIsNone(resolve_market_direction(None, "SELL")["direction"])

    def test_reversal_rebuilds_stop_target_from_final_side(self):
        cfg = SimpleNamespace(SL_BUFFER_PCT=0.05, SL_BUFFER_PCT_SELL=None,
                              RISK_REWARD_MIN=2.0)
        candle = pd.Series({"low": 99.0, "high": 101.0})
        raw_sell = Signal("TEST", "SELL", 100.0, 101.05, 97.9,
                          pd.Timestamp("2026-08-23 10:00"), "raw")
        rebuilt = rebuild_signal_for_direction(raw_sell, "BUY", candle, cfg)
        self.assertEqual(rebuilt.direction, "BUY")
        self.assertLess(rebuilt.stop_loss, rebuilt.entry_price)
        self.assertLess(rebuilt.entry_price, rebuilt.target)
        self.assertNotEqual(rebuilt.stop_loss, 101.05)
        risk = RiskManager(SimpleNamespace(
            CAPITAL=100000, RISK_PER_TRADE_PCT=1.0,
            MAX_DAILY_LOSS_PCT=3.0, MAX_TRADES_PER_DAY=4,
            MAX_OPEN_POSITIONS=5), persist=False)
        expected_qty = int(1000 / abs(rebuilt.entry_price - rebuilt.stop_loss))
        self.assertEqual(
            risk.position_size(rebuilt.entry_price, rebuilt.stop_loss), expected_qty
        )

        raw_buy = Signal("TEST", "BUY", 100.0, 98.95, 102.1,
                         pd.Timestamp("2026-08-23 10:00"), "raw")
        rebuilt = rebuild_signal_for_direction(raw_buy, "SELL", candle, cfg)
        self.assertEqual(rebuilt.direction, "SELL")
        self.assertLess(rebuilt.target, rebuilt.entry_price)
        self.assertLess(rebuilt.entry_price, rebuilt.stop_loss)
        self.assertNotEqual(rebuilt.stop_loss, 98.95)


if __name__ == "__main__":
    unittest.main()
