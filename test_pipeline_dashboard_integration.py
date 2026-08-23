from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, render_template_string

import pipeline_dashboard as dashboard
from monitor_route import MONITOR_PAGE


class PipelineDashboardIntegrationTests(unittest.TestCase):
    def test_selector_events_services_and_limits_join_one_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "latest_report.json"
            events = root / "pipeline_events.jsonl"
            report.write_text(json.dumps({
                "status": "success",
                "strategy": "NSE_MOMENTUM_RVOL_TOP120",
                "generated_at": "2026-08-24T09:40:00+05:30",
                "eligible_count": 500,
                "selected": [{
                    "symbol": "AAA", "score": 100, "momentum_pct": 1.25,
                    "relative_volume": 1.75, "sweet_spot_distance": 0.0,
                }],
            }), encoding="utf-8")
            events.write_text(json.dumps({
                "recorded_at": "2026-08-24T10:00:00+05:30", "symbol": "AAA",
                "market": "BEARISH", "raw_direction": "SELL", "decision": "REVERSE",
                "final_direction": "BUY", "status": "CANDIDATE",
            }) + "\n", encoding="utf-8")
            fake = types.SimpleNamespace(stdout="active\n")
            with patch.object(dashboard, "SELECTOR_REPORT", report), \
                 patch.object(dashboard, "PIPELINE_EVENTS", events), \
                 patch.object(dashboard.subprocess, "run", return_value=fake):
                result = dashboard.load_pipeline_dashboard()
            self.assertEqual(result["selector"]["selected_count"], 1)
            self.assertEqual(result["selector"]["top"][0]["score"], 100)
            self.assertEqual(result["recent_decisions"][0]["final_direction"], "BUY")
            self.assertEqual(result["services"]["watchlist_timer"], "active")
            self.assertEqual(result["limits"]["force_square_off"], "15:08 IST")

    def test_telemetry_write_failure_never_reaches_trading(self):
        with patch.object(dashboard.os, "open", side_effect=OSError("disk unavailable")):
            dashboard.record_pipeline_event(symbol="AAA", status="CANDIDATE")

    def test_monitor_renders_pipeline_and_direction_columns(self):
        app = Flask(__name__)
        pipeline = {
            "services": {"live_bot": "inactive", "watchlist_timer": "active"},
            "selector": {"status": "success", "fresh_today": True, "selected_count": 120, "top": []},
            "strategy": {"market_policy": "Bearish→BUY; Bullish→raw; Sideways→SELL; Unknown→skip"},
            "limits": {"risk_per_trade_pct": 2.0, "max_trades_per_day": 10, "max_open_positions": 1, "max_daily_loss_pct": 0.5, "force_square_off": "15:08 IST"},
            "recent_decisions": [{"symbol": "AAA", "market": "BEARISH", "raw_direction": "SELL", "decision": "REVERSE", "final_direction": "BUY", "status": "CANDIDATE"}],
        }
        with app.app_context():
            html = render_template_string(
                MONITOR_PAGE, updated="now", positions=[], portfolio={}, session={}, health={},
                profit_factor_display="N/A", watchlist_snapshot=None,
                freshness={"status":"NO_REPORT_AVAILABLE"}, summary_cards={},
                watchlist_symbols_json="[]", pipeline=pipeline,
            )
        self.assertIn("Clean Pipeline Integration", html)
        self.assertIn("Top-120 Loaded", html)
        self.assertIn("REVERSE", html)
        self.assertIn("Raw", html)
        self.assertIn("Final", html)

    def test_dashboard_supports_lower_and_uppercase_monitor_urls(self):
        source = Path("configure_app.py").read_text(encoding="utf-8")
        self.assertIn('@app.route("/monitor")', source)
        self.assertIn('@app.route("/Monitor")', source)


if __name__ == "__main__":
    unittest.main()
