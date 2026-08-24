from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import live_combined_preflight as preflight
from triple_pattern_policy import evaluate_confirmed_triple_pattern


ROOT = Path(__file__).resolve().parent


def load_isolated_launcher(config):
    fake_policy = types.ModuleType("paper_contrarian_launcher")
    fake_policy.LIVE_ACK_ENV = "KITE_LIVE_COMBINED_ACK"
    fake_policy.LIVE_ACK_VALUE = "I_ACCEPT_REAL_ORDERS"
    fake_policy.install_two_indicator_patch = lambda **kwargs: None
    fake_breakout = types.ModuleType("directional_breakout_validator")
    fake_breakout.validate_breakout = lambda *args, **kwargs: None
    with patch.dict(
        sys.modules,
        {
            "config": config,
            "paper_contrarian_launcher": fake_policy,
            "directional_breakout_validator": fake_breakout,
        },
    ):
        spec = importlib.util.spec_from_file_location(
            "isolated_combined_live_launcher", ROOT / "combined_live_launcher.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class CombinedLiveLauncherTests(unittest.TestCase):
    def config(self, *, paper=False):
        return types.SimpleNamespace(
            PAPER_TRADING=paper,
            PRODUCT="MIS",
            CAPITAL=5000.0,
            MARKET_PROTECTION=-1,
            RISK_PER_TRADE_PCT=99.0,
            MAX_OPEN_POSITIONS=99,
            MAX_TRADES_PER_DAY=99,
            MAX_DAILY_LOSS_PCT=99.0,
            MAX_POSITION_SIZE_PCT=99.0,
            CHECK_MARGIN_BEFORE_ENTRY=False,
            ENABLE_FIXED_TARGET=False,
            ENABLE_TRAILING_STOP=True,
            EXIT_IMMEDIATELY_AT_TARGET=False,
        )

    def test_acknowledgement_is_mandatory(self):
        module = load_isolated_launcher(self.config())
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                module.enforce_live_limits()

    def test_paper_mode_is_rejected_even_with_ack(self):
        module = load_isolated_launcher(self.config(paper=True))
        with patch.dict(
            os.environ,
            {"KITE_LIVE_COMBINED_ACK": "I_ACCEPT_REAL_ORDERS"},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                module.enforce_live_limits()

    def test_live_limits_are_hard_capped(self):
        config = self.config()
        module = load_isolated_launcher(config)
        with patch.dict(
            os.environ,
            {"KITE_LIVE_COMBINED_ACK": "I_ACCEPT_REAL_ORDERS"},
            clear=True,
        ):
            limits = module.enforce_live_limits()
        self.assertEqual(limits["risk_per_trade_pct"], 2.0)
        self.assertEqual(limits["max_open_positions"], 1)
        self.assertEqual(limits["max_trades_per_day"], 10)
        self.assertFalse(limits["daily_loss_kill_switch_enabled"])
        self.assertEqual(limits["max_consecutive_losses"], 3)
        self.assertEqual(limits["max_position_size_pct"], 50.0)
        self.assertTrue(config.CHECK_MARGIN_BEFORE_ENTRY)
        self.assertEqual(config.ENTRY_SCAN_SHORTLIST_SIZE, 120)
        self.assertTrue(config.ENABLE_FIXED_TARGET)
        self.assertFalse(config.ENABLE_TRAILING_STOP)
        self.assertTrue(config.ENABLE_DEPTH_CONFIRMATION_GATE)
        self.assertEqual(config.DEPTH_CONFIRMATION_WINDOW_SECONDS, 30.0)
        self.assertEqual(config.DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS, 15.0)
        self.assertEqual(config.DEPTH_CONFIRMATION_IMBALANCE, 0.20)
        self.assertEqual(config.DEPTH_CONFIRMATION_PERSISTENCE, 0.70)
        self.assertEqual(config.DEPTH_CONFIRMATION_MAX_SPREAD_BPS, 5.0)
        self.assertTrue(limits["depth_confirmation_gate"])


class PatternModeTests(unittest.TestCase):
    def test_live_pattern_requires_explicit_allow_live(self):
        live = types.SimpleNamespace(PAPER_TRADING=False, PAPER_ENABLE_TRIPLE_PATTERN=True)
        blocked = evaluate_confirmed_triple_pattern(None, live)
        allowed_path = evaluate_confirmed_triple_pattern(None, live, allow_live=True)
        self.assertEqual(blocked.reasons, ["PAPER_ONLY"])
        self.assertEqual(allowed_path.reasons, ["INSUFFICIENT_OR_MISSING_DATA"])

    def test_live_override_cannot_run_in_paper_mode(self):
        paper = types.SimpleNamespace(PAPER_TRADING=True, PAPER_ENABLE_TRIPLE_PATTERN=True)
        result = evaluate_confirmed_triple_pattern(None, paper, allow_live=True)
        self.assertEqual(result.reasons, ["LIVE_MODE_REQUIRED"])


class LivePreflightTests(unittest.TestCase):
    def artifacts(self):
        selected = [
            {
                "symbol": f"S{i:02d}",
                "exchange": "NSE",
                "ordinary_equity_clean": True,
                "momentum_pct": 1.25,
                "relative_volume": 1.75,
                "score": 100,
                "sweet_spot_distance": 0.0,
                "turnover": 120 - i,
            }
            for i in range(preflight.EXPECTED_WATCHLIST_SIZE)
        ]
        report = {
            "status": "success",
            "strategy": preflight.STRATEGY_NAME,
            "mode": "READ_ONLY",
            "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
            "selected": selected,
        }
        payload = {
            "status": "success",
            "strategy": preflight.STRATEGY_NAME,
            "watchlist": [
                {"symbol": row["symbol"], "exchange": row["exchange"]}
                for row in selected
            ],
        }
        return report, payload

    def test_selector_handoff_requires_exactly_120(self):
        report, payload = self.artifacts()
        self.assertEqual(
            len(preflight.validate_selector_artifacts(report, payload)),
            preflight.EXPECTED_WATCHLIST_SIZE,
        )
        payload["watchlist"].pop()
        with self.assertRaises(RuntimeError):
            preflight.validate_selector_artifacts(report, payload)

    def test_atomic_handoff_preserves_live_mode(self):
        report, payload = self.artifacts()
        watchlist = preflight.validate_selector_artifacts(report, payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "user_config.json"
            config.write_text(
                json.dumps({"paper_trading": False, "watchlist": []}),
                encoding="utf-8",
            )
            backup = preflight.atomic_apply_watchlist(config, watchlist, root / "backups")
            updated = json.loads(config.read_text(encoding="utf-8"))
            self.assertFalse(updated["paper_trading"])
            self.assertEqual(
                len(updated["watchlist"]), preflight.EXPECTED_WATCHLIST_SIZE
            )
            self.assertTrue(backup.exists())

    def test_broker_flat_check_rejects_mis_exposure(self):
        kite = types.SimpleNamespace(
            positions=lambda: {
                "net": [{"product": "MIS", "quantity": 1, "tradingsymbol": "INFY"}],
                "day": [],
            },
            orders=lambda: [],
        )
        with self.assertRaises(RuntimeError):
            preflight.validate_broker_flat(kite)


class DeploymentContractTests(unittest.TestCase):
    def test_live_timer_is_separate_and_not_persistent(self):
        timer = (ROOT / "systemd" / "kite-live-watchlist.timer").read_text()
        self.assertIn("09:26:50 Asia/Kolkata", timer)
        self.assertIn("Persistent=false", timer)
        self.assertIn("kite-live-watchlist.service", timer)

    def test_live_watchlist_allows_full_nse_history_baseline_run(self):
        unit = (ROOT / "systemd" / "kite-live-watchlist.service").read_text()
        self.assertIn("TimeoutStartSec=1800", unit)

    def test_live_service_uses_only_guarded_launcher(self):
        unit = (ROOT / "systemd" / "kitebot-live-combined.service").read_text()
        self.assertIn("combined_live_launcher.py", unit)
        self.assertIn("Conflicts=kitebot-paper-contrarian.service kitebot.service", unit)

    def test_daily_stop_includes_combined_live_service(self):
        unit = (ROOT / "systemd" / "kitebot-stop.service").read_text()
        self.assertIn("kitebot-live-combined.service", unit)

    def test_live_clean_pipeline_makes_legacy_strategy_checks_observational(self):
        source = (ROOT / "paper_contrarian_launcher.py").read_text()
        self.assertIn("EMA9/EMA21 on the completed 3-minute stock", source)
        self.assertIn('"legacy_gates": "OBSERVATIONAL_ONLY"', source)
        self.assertIn('confidence="EMA_RAW"', source)


if __name__ == "__main__":
    unittest.main()
