import sys
import types
import unittest
from types import SimpleNamespace

import pandas as pd

protective_stop = types.ModuleType("protective_stop")
protective_stop.place_protective_stop = lambda *args, **kwargs: None
sys.modules.setdefault("protective_stop", protective_stop)

from entry_protection import build_confirmed_position, build_entry_plan
from triple_pattern_policy import (
    TRIPLE_BOTTOM,
    TRIPLE_TOP,
    evaluate_confirmed_triple_pattern,
)


def frame_for(pattern, *, volume=40.0, aligned=True):
    if pattern == TRIPLE_TOP:
        closes = [
            96, 97, 98, 99, 100, 99, 97, 95, 94, 96, 98, 99, 100.1,
            99, 97, 95, 94.1, 96, 98, 99, 99.9, 99, 97, 95, 94.2,
            95, 96, 95,
        ]
        breakout_close = 93.7
        breakout_vwap = 96.0 if aligned else 92.0
    else:
        closes = [
            104, 103, 102, 101, 100, 101, 103, 105, 106, 104, 102, 101,
            99.9, 101, 103, 105, 105.9, 104, 102, 101, 100.1, 101, 103,
            105, 105.8, 105, 104, 105,
        ]
        breakout_close = 106.5
        breakout_vwap = 104.0 if aligned else 108.0

    start = pd.Timestamp("2026-08-17 09:15", tz="Asia/Kolkata")
    rows = [
        {
            "date": start + pd.Timedelta(minutes=3 * index),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 20.0,
            "vwap": 102.0,
        }
        for index, close in enumerate(closes)
    ]
    rows.append({
        "date": start + pd.Timedelta(minutes=3 * len(closes)),
        "open": closes[-1],
        "high": max(closes[-1], breakout_close) + 0.2,
        "low": min(closes[-1], breakout_close) - 0.2,
        "close": breakout_close,
        "volume": volume,
        "vwap": breakout_vwap,
    })
    return pd.DataFrame(rows)


class TriplePatternPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            PAPER_TRADING=True,
            PAPER_ENABLE_TRIPLE_PATTERN=True,
            PAPER_TRIPLE_PATTERN_MIN_VOLUME_RATIO=1.5,
            PAPER_TRIPLE_PATTERN_STOP_PERCENT=0.45,
            PAPER_TRIPLE_TOP_TARGET_PERCENT=1.0,
            PAPER_TRIPLE_BOTTOM_TARGET_PERCENT=2.0,
            ENTRY_TIMEFRAME="3minute",
            PAPER_CANDLE_COMPLETION_GRACE_SECONDS=5.0,
            PAPER_CANDLE_MAX_FRESH_SECONDS=90.0,
        )

    def evaluate(self, frame):
        now = frame.iloc[-1]["date"] + pd.Timedelta(minutes=3, seconds=10)
        return evaluate_confirmed_triple_pattern(frame, self.cfg, now=now)

    def test_confirmed_triple_top(self):
        result = self.evaluate(frame_for(TRIPLE_TOP))
        self.assertTrue(result.accepted)
        self.assertEqual(result.pattern, TRIPLE_TOP)
        self.assertEqual(result.direction, "SELL")
        self.assertEqual(result.profit_target_percent, 1.0)

    def test_confirmed_triple_bottom(self):
        result = self.evaluate(frame_for(TRIPLE_BOTTOM))
        self.assertTrue(result.accepted)
        self.assertEqual(result.pattern, TRIPLE_BOTTOM)
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.profit_target_percent, 2.0)

    def test_volume_and_vwap_are_mandatory(self):
        low_volume = self.evaluate(frame_for(TRIPLE_TOP, volume=25.0))
        wrong_vwap = self.evaluate(frame_for(TRIPLE_BOTTOM, aligned=False))
        self.assertFalse(low_volume.accepted)
        self.assertIn("TRIPLE_PATTERN_VOLUME_BELOW_MINIMUM", low_volume.reasons)
        self.assertFalse(wrong_vwap.accepted)
        self.assertIn("TRIPLE_PATTERN_VWAP_NOT_ALIGNED", wrong_vwap.reasons)

    def test_live_mode_fails_closed(self):
        self.cfg.PAPER_TRADING = False
        result = self.evaluate(frame_for(TRIPLE_TOP))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ["PAPER_ONLY"])

    def test_forming_or_stale_candle_fails_closed(self):
        frame = frame_for(TRIPLE_TOP)
        forming = evaluate_confirmed_triple_pattern(
            frame,
            self.cfg,
            now=frame.iloc[-1]["date"] + pd.Timedelta(minutes=2),
        )
        stale = evaluate_confirmed_triple_pattern(
            frame,
            self.cfg,
            now=frame.iloc[-1]["date"] + pd.Timedelta(minutes=5),
        )
        self.assertEqual(forming.reasons, ["CANDLE_NOT_COMPLETED_OR_FRESH"])
        self.assertEqual(stale.reasons, ["CANDLE_NOT_COMPLETED_OR_FRESH"])


class PatternExitHandoffTests(unittest.TestCase):
    def test_confirmed_fill_reanchors_fixed_target_and_skips_hybrid(self):
        cfg = SimpleNamespace(
            PAPER_TRADING=True,
            ENABLE_FIXED_TARGET=True,
            ENABLE_HYBRID_EXIT=True,
            STOP_LOSS_PERCENT=0.45,
            PROFIT_TARGET_PERCENT=0.70,
        )
        signal = SimpleNamespace(
            direction="BUY",
            entry_price=100.0,
            stop_loss=99.55,
            target=102.0,
            timestamp="2026-08-17 10:42:00+05:30",
            price_action_detail={
                "trade_policy": {
                    "exit_policy": "PATTERN_FIXED",
                    "pattern": "TRIPLE_BOTTOM",
                    "stop_loss_percent": 0.45,
                    "profit_target_percent": 2.0,
                }
            },
        )
        plan = build_entry_plan(signal, cfg)
        self.assertEqual(plan["profit_target_percent"], 2.0)
        position = build_confirmed_position(
            signal,
            {"filled_quantity": 10, "average_price": 101.0, "status": "COMPLETE"},
            "NSE",
            cfg,
        )
        self.assertAlmostEqual(position["stop"], 101.0 * 0.9955)
        self.assertAlmostEqual(position["target"], 101.0 * 1.02)
        self.assertFalse(position["hybrid_exit_enabled"])


if __name__ == "__main__":
    unittest.main()
