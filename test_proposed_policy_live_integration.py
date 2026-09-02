import unittest
from types import SimpleNamespace

import pandas as pd

from entry_protection import build_confirmed_position, build_entry_plan
from market_direction_policy import resolve_market_direction
from risk_manager import RiskManager
from strategy import Signal, rebuild_signal_for_direction


class ProposedPolicyLiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            SL_BUFFER_PCT=0.05, SL_BUFFER_PCT_SELL=None,
            RISK_REWARD_MIN=2.0, CAPITAL=100000,
            RISK_PER_TRADE_PCT=1.0, MAX_DAILY_LOSS_PCT=3.0,
            MAX_TRADES_PER_DAY=5, MAX_OPEN_POSITIONS=1,
            PAPER_TRADING=False, ENABLE_FIXED_TARGET=True,
            STOP_LOSS_PERCENT=0.45, PROFIT_TARGET_PERCENT=1.5,
            ENABLE_HYBRID_EXIT=False,
        )

    def _reversed(self, market, raw):
        result = resolve_market_direction(market, raw)
        signal = Signal("TEST", raw, 100.0, 90.0 if raw == "BUY" else 110.0,
                        120.0 if raw == "BUY" else 80.0,
                        pd.Timestamp("2026-08-24 09:30"), "raw")
        signal.raw_direction = raw
        signal.policy_decision = result["decision"]
        signal.policy_reason = result["reason"]
        signal.market_trend = result["market"]
        return rebuild_signal_for_direction(
            signal, result["direction"], pd.Series({"low": 99.0, "high": 101.0}), self.cfg
        )

    def test_sell_to_buy_rebuilds_every_downstream_value(self):
        signal = self._reversed("BEARISH", "SELL")
        self.assertEqual(signal.direction, "BUY")
        self.assertLess(signal.stop_loss, signal.entry_price)
        qty = RiskManager(self.cfg, persist=False).position_size(
            signal.entry_price, signal.stop_loss
        )
        self.assertGreater(qty, 0)
        analytics = {"raw_direction": "SELL", "final_direction": "BUY",
                     "policy_decision": "REVERSE", "policy_reason": signal.policy_reason,
                     "policy_market_trend": "BEARISH"}
        plan = build_entry_plan(signal, self.cfg, signal_analytics=analytics)
        self.assertLess(plan["signal_stop_price"], plan["signal_entry_price"])
        position = build_confirmed_position(
            signal,
            {"filled_quantity": qty, "average_price": 100.2, "status": "COMPLETE"},
            "NSE", self.cfg, signal_analytics=analytics,
        )
        self.assertEqual(position["direction"], "BUY")
        self.assertEqual(position["raw_direction"], "SELL")
        self.assertEqual(position["final_direction"], "BUY")
        self.assertLess(position["stop"], position["entry"])

    def test_buy_to_sell_rebuilds_every_downstream_value(self):
        signal = self._reversed("SIDEWAYS", "BUY")
        self.assertEqual(signal.direction, "SELL")
        self.assertLess(signal.target, signal.entry_price)
        position = build_confirmed_position(
            signal,
            {"filled_quantity": 10, "average_price": 99.8, "status": "COMPLETE"},
            "NSE", self.cfg,
            signal_analytics={"raw_direction": "BUY", "final_direction": "SELL"},
        )
        self.assertEqual(position["direction"], "SELL")
        self.assertGreater(position["stop"], position["entry"])


if __name__ == "__main__":
    unittest.main()
