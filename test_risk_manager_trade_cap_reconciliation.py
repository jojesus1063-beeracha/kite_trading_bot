import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import risk_manager as rm


def _cfg(limit):
    return SimpleNamespace(
        CAPITAL=5000.0,
        MAX_DAILY_LOSS_PCT=5.0,
        RISK_PER_TRADE_PCT=0.20,
        MAX_TRADES_PER_DAY=limit,
        MAX_OPEN_POSITIONS=2,
    )


def _state(*, trades, reason):
    return {
        "date": date.today().isoformat(),
        "trades_taken": trades,
        "realized_pnl": 0.0,
        "halted": True,
        "halt_reason": reason,
    }


class TradeCapReconciliationTests(unittest.TestCase):
    def _load(self, state, current_limit):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "day_state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(rm, "DAY_STATE_PATH", str(path)):
            risk = rm.RiskManager(_cfg(current_limit), persist=True)
            saved = json.loads(path.read_text(encoding="utf-8"))
        return risk, saved

    def test_raised_cap_clears_obsolete_trade_count_halt(self):
        risk, saved = self._load(
            _state(trades=5, reason="Max trades per day (5) reached"),
            current_limit=20,
        )
        self.assertFalse(risk.day.halted)
        self.assertEqual(risk.day.halt_reason, "")
        self.assertFalse(saved["halted"])
        self.assertTrue(risk.can_take_new_trade())

    def test_same_cap_retains_trade_count_halt(self):
        risk, _ = self._load(
            _state(trades=5, reason="Max trades per day (5) reached"),
            current_limit=5,
        )
        self.assertTrue(risk.day.halted)
        self.assertFalse(risk.can_take_new_trade())

    def test_daily_loss_halt_remains_sticky(self):
        risk, saved = self._load(
            _state(trades=5, reason="Daily loss limit (5.0% of capital) hit"),
            current_limit=20,
        )
        self.assertTrue(risk.day.halted)
        self.assertTrue(saved["halted"])
        self.assertFalse(risk.can_take_new_trade())


if __name__ == "__main__":
    unittest.main()
