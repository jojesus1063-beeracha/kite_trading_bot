import json
import tempfile
import unittest
from pathlib import Path

from compare_breakout_all_trades import cohort_summary, load_trades


class BreakoutAllTradesComparisonTests(unittest.TestCase):
    def test_partial_exit_legs_are_aggregated(self):
        rows = [
            {
                "date": "2026-08-14",
                "time": "10:00:00",
                "symbol": "ABC",
                "exchange": "NSE",
                "direction": "BUY",
                "entry": 100.0,
                "entry_time": "2026-08-14T09:30:00+05:30",
                "signal_id": "one-signal",
                "pnl": 4.0,
                "gross_pnl": 5.0,
                "costs": 1.0,
                "result": "hybrid_scalp_1r",
            },
            {
                "date": "2026-08-14",
                "time": "10:10:00",
                "symbol": "ABC",
                "exchange": "NSE",
                "direction": "BUY",
                "entry": 100.0,
                "entry_time": "2026-08-14T09:30:00+05:30",
                "signal_id": "one-signal",
                "pnl": -2.0,
                "gross_pnl": -1.0,
                "costs": 1.0,
                "result": "breakeven_stop",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            trades = load_trades(path)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].legs, 2)
        self.assertEqual(trades[0].net_pnl, 2.0)
        self.assertEqual(trades[0].gross_pnl, 4.0)
        self.assertEqual(trades[0].costs, 2.0)

    def test_summary_uses_net_pnl(self):
        summary = cohort_summary([
            {"net_pnl": 10.0},
            {"net_pnl": -4.0},
            {"net_pnl": 0.0},
        ])
        self.assertEqual(summary["trades"], 3)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["net_pnl"], 6.0)


if __name__ == "__main__":
    unittest.main()
