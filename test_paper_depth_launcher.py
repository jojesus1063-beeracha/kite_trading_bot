from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def load_launcher(config):
    fake_policy = types.ModuleType("paper_contrarian_launcher")
    fake_policy.install_two_indicator_patch = lambda: None
    fake_breakout = types.ModuleType("directional_breakout_validator")
    fake_breakout.validate_breakout = lambda *args, **kwargs: None
    fake_entry_quality = types.ModuleType("entry_quality")
    fake_entry_quality.MAX_EMA_DISTANCE_ATR = 99.0
    fake_price_action = types.ModuleType("price_action")
    fake_price_action.validate_breakout = None
    with patch.dict(
        sys.modules,
        {
            "config": config,
            "entry_quality": fake_entry_quality,
            "price_action": fake_price_action,
            "paper_contrarian_launcher": fake_policy,
            "directional_breakout_validator": fake_breakout,
        },
    ):
        spec = importlib.util.spec_from_file_location(
            "isolated_paper_depth_launcher", ROOT / "paper_depth_launcher.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, fake_entry_quality


def config(*, paper=True, websocket=True):
    return types.SimpleNamespace(
        PAPER_TRADING=paper,
        ENABLE_WS_CANDLES=websocket,
        CAPITAL=5000.0,
        RISK_PER_TRADE_PCT=99.0,
        MAX_OPEN_POSITIONS=99,
        MAX_TRADES_PER_DAY=99,
        MAX_DAILY_LOSS_PCT=99.0,
        MAX_POSITION_SIZE_PCT=99.0,
        CHECK_MARGIN_BEFORE_ENTRY=True,
        ENABLE_FIXED_TARGET=False,
        ENABLE_TRAILING_STOP=True,
        EXIT_IMMEDIATELY_AT_TARGET=False,
    )


class PaperDepthLauncherTests(unittest.TestCase):
    def test_live_mode_is_rejected(self):
        module, _ = load_launcher(config(paper=False))
        with self.assertRaises(SystemExit):
            module.enforce_paper_depth_settings()

    def test_websocket_is_mandatory(self):
        module, _ = load_launcher(config(websocket=False))
        with self.assertRaises(SystemExit):
            module.enforce_paper_depth_settings()

    def test_settings_match_live_depth_experiment_without_real_orders(self):
        cfg, = (config(),)
        module, entry_quality = load_launcher(cfg)
        settings = module.enforce_paper_depth_settings()
        self.assertTrue(cfg.PAPER_TRADING)
        self.assertEqual(cfg.CAPITAL, 100_000.0)
        self.assertEqual(cfg.RISK_PER_TRADE_PCT, 2.0)
        self.assertEqual(cfg.MAX_OPEN_POSITIONS, 120)
        self.assertEqual(cfg.MAX_TRADES_PER_DAY, 999)
        self.assertFalse(cfg.DAILY_LOSS_KILL_SWITCH_ENABLED)
        self.assertEqual(cfg.MAX_CONSECUTIVE_LOSSES, 0)
        self.assertFalse(cfg.CHECK_MARGIN_BEFORE_ENTRY)
        self.assertTrue(cfg.PROPOSED_CLEAN_PIPELINE)
        self.assertEqual(cfg.ENTRY_SCAN_SHORTLIST_SIZE, 120)
        self.assertEqual(entry_quality.MAX_EMA_DISTANCE_ATR, 2.0)
        self.assertTrue(cfg.ENABLE_DEPTH_CONFIRMATION_GATE)
        self.assertEqual(cfg.DEPTH_CONFIRMATION_WINDOW_SECONDS, 30.0)
        self.assertEqual(cfg.DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS, 15.0)
        self.assertEqual(cfg.DEPTH_CONFIRMATION_IMBALANCE, 0.20)
        self.assertEqual(cfg.DEPTH_CONFIRMATION_PERSISTENCE, 0.70)
        self.assertEqual(cfg.DEPTH_CONFIRMATION_MAX_SPREAD_BPS, 5.0)
        self.assertTrue(settings["paper_trading"])
        self.assertEqual(settings["capital"], 100_000.0)

    def test_systemd_unit_uses_paper_depth_launcher_and_conflicts_with_live(self):
        unit = (ROOT / "systemd" / "kitebot-paper-contrarian.service").read_text()
        self.assertIn("paper_depth_launcher.py", unit)
        self.assertIn("kitebot-live-combined.service", unit)

    def test_paper_selector_requires_exactly_120(self):
        script = (ROOT / "run_paper_watchlist_daily.sh").read_text()
        self.assertIn("--top 120", script)
        self.assertIn("--min-selected 120", script)
        self.assertIn("expected exactly 120 stocks", script)


if __name__ == "__main__":
    unittest.main()
