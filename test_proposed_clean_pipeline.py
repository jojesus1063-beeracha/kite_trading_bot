from __future__ import annotations

from contextlib import ExitStack
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

breakout_diagnostics = types.ModuleType("breakout_diagnostics")
breakout_diagnostics.enrich_breakout_diagnostics = lambda event: event
sys.modules.setdefault("breakout_diagnostics", breakout_diagnostics)

import config as cfg
import paper_contrarian_launcher as launcher
import strategy
from live_momentum_rvol_selector import momentum_rvol_score, rank_candidates


class ProposedCleanPipelineTests(unittest.TestCase):
    def test_frozen_momentum_rvol_score_table(self):
        cases = [
            (1.25, 1.75, 100), (1.25, 2.50, 90), (1.25, 1.25, 80),
            (0.85, 2.00, 70), (1.20, 0.85, 60), (0.70, 1.20, 50),
            (1.75, 1.50, 35), (2.00, 1.50, 20), (1.25, 3.00, 20),
            (0.20, 0.20, 10),
        ]
        for momentum, rvol, expected in cases:
            with self.subTest(momentum=momentum, rvol=rvol):
                self.assertEqual(momentum_rvol_score(momentum, rvol), expected)

    def test_score_then_sweet_spot_distance_controls_ranking(self):
        rows = [
            {"symbol": "FAR", "score": 100, "sweet_spot_distance": 2.0, "turnover": 9},
            {"symbol": "NEAR", "score": 100, "sweet_spot_distance": 0.1, "turnover": 1},
            {"symbol": "LOW", "score": 90, "sweet_spot_distance": 0.0, "turnover": 99},
        ]
        self.assertEqual(
            [row["symbol"] for row in rank_candidates(rows)],
            ["NEAR", "FAR", "LOW"],
        )

    def test_ema_is_sole_raw_signal_when_legacy_observations_fail(self):
        original_evaluate = strategy.evaluate
        attributes = {
            "PAPER_TRADING": True,
            "NO_ENTRY_BEFORE": "09:25",
            "NO_ENTRY_AFTER": "15:00",
            "ENTRY_TIMEFRAME": "3minute",
            "STOP_LOSS_PERCENT": 0.45,
            "PROFIT_TARGET_PERCENT": 0.70,
        }
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(launcher, "_config_snapshot", lambda: None))
                stack.enter_context(patch.object(launcher, "_append", lambda event: None))
                for name, value in attributes.items():
                    stack.enter_context(patch.object(cfg, name, value, create=True))
                launcher.install_two_indicator_patch(live_combined=False)
                start = pd.Timestamp("2026-08-24 09:27", tz="Asia/Kolkata")
                frame = pd.DataFrame({
                    "date": [start + pd.Timedelta(minutes=3 * i) for i in range(30)],
                    "open": [100 + i * 0.1 for i in range(30)],
                    "high": [100.2 + i * 0.1 for i in range(30)],
                    "low": [99.8 + i * 0.1 for i in range(30)],
                    "close": [100 + i * 0.1 for i in range(30)],
                    "volume": [1.0] * 30,
                    "vwap": [999.0] * 30,
                })
                signal = strategy.evaluate("TEST", pd.DataFrame(), frame, pd.DataFrame(), cfg)
                self.assertIsNotNone(signal)
                self.assertEqual(signal.direction, "BUY")
                self.assertEqual(signal.confidence, "EMA_RAW")
                self.assertTrue(signal.price_action_detail["observational_only"])
        finally:
            strategy.evaluate = original_evaluate

    def test_live_timer_uses_only_new_selector_contract(self):
        source = Path("run_live_combined_watchlist_daily.sh").read_text(encoding="utf-8")
        self.assertIn("live_momentum_rvol_selector.py", source)
        self.assertNotIn("paper_full_universe_top60_selector.py", source)
        self.assertNotIn("--min-turnover", source)
        self.assertNotIn("--max-price", source)

    def test_main_preserves_watchlist_order_and_guards_legacy_vetoes(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("ranked_candidates = list(entry_candidates)", source)
        self.assertGreaterEqual(source.count('not getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False)'), 5)


if __name__ == "__main__":
    unittest.main()
