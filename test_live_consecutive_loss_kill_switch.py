from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import risk_manager as rm


def _live_cfg():
    return SimpleNamespace(
        CAPITAL=5000.0,
        RISK_PER_TRADE_PCT=2.0,
        MAX_DAILY_LOSS_PCT=0.5,
        DAILY_LOSS_KILL_SWITCH_ENABLED=False,
        MAX_CONSECUTIVE_LOSSES=3,
        MAX_TRADES_PER_DAY=10,
        MAX_OPEN_POSITIONS=1,
    )


class LiveConsecutiveLossKillSwitchTests(unittest.TestCase):
    def test_cumulative_daily_loss_no_longer_halts_live_mode(self):
        risk = rm.RiskManager(_live_cfg(), persist=False)
        risk.record_trade_result(-100.0)
        self.assertFalse(risk.day.halted)
        self.assertTrue(risk.can_take_new_trade())

    def test_third_consecutive_loss_halts(self):
        risk = rm.RiskManager(_live_cfg(), persist=False)
        risk.record_trade_result(-10.0)
        risk.record_trade_result(-20.0)
        self.assertFalse(risk.day.halted)
        risk.record_trade_result(-1.0)
        self.assertTrue(risk.day.halted)
        self.assertEqual(risk.day.consecutive_losses, 3)
        self.assertEqual(risk.day.halt_reason, "Max consecutive losses (3) reached")

    def test_non_loss_resets_streak(self):
        risk = rm.RiskManager(_live_cfg(), persist=False)
        risk.record_trade_result(-10.0)
        risk.record_trade_result(-20.0)
        risk.record_trade_result(0.0)
        risk.record_trade_result(-30.0)
        self.assertEqual(risk.day.consecutive_losses, 1)
        self.assertFalse(risk.day.halted)

    def test_streak_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "day_state.json"
            with patch.object(rm, "DAY_STATE_PATH", str(state_path)):
                first = rm.RiskManager(_live_cfg(), persist=True)
                first.record_trade_result(-10.0)
                first.record_trade_result(-20.0)
                restored = rm.RiskManager(_live_cfg(), persist=True)
                self.assertEqual(restored.day.consecutive_losses, 2)
                restored.record_trade_result(-30.0)
                saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["halted"])
            self.assertEqual(saved["consecutive_losses"], 3)


if __name__ == "__main__":
    unittest.main()
