import unittest
from unittest.mock import patch

import pandas as pd

import config as cfg
from replay_preopen_static_watchlist_day import (
    Candidate,
    configure_replay,
    session_candidates,
    technical_decision,
)


class PreopenStaticWatchlistReplayTests(unittest.TestCase):
    def setUp(self):
        configure_replay()
        cfg.NO_ENTRY_BEFORE = "09:25"
        cfg.NO_ENTRY_AFTER = "15:00"

    def test_session_candidates_are_exchange_time_bounded(self):
        times = pd.to_datetime(
            [
                "2026-08-14 09:24:00+05:30",
                "2026-08-14 09:27:00+05:30",
                "2026-08-14 15:00:00+05:30",
                "2026-08-14 15:03:00+05:30",
            ]
        )
        frame = pd.DataFrame({
            "date": times,
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.5] * 4,
            "volume": [1000] * 4,
        })

        candidates = session_candidates(
            frame, "2026-08-14", "TEST", "NSE"
        )

        self.assertEqual(
            [item.timestamp.strftime("%H:%M") for item in candidates],
            ["09:27", "15:00"],
        )

    def test_replay_enables_hard_breakout_and_current_caps(self):
        self.assertTrue(cfg.PAPER_REQUIRE_VALIDATED_BREAKOUT)
        self.assertEqual(cfg.PAPER_MAX_ENTRIES_PER_DAY, 20)
        self.assertEqual(cfg.MAX_OPEN_POSITIONS, 2)
        self.assertEqual(cfg.PAPER_MAX_TRADES_PER_SYMBOL, 2)

    def test_non_breakout_short_circuits_expensive_price_action(self):
        timestamp = pd.Timestamp("2026-08-14 10:00:00", tz="Asia/Kolkata")
        dates = pd.date_range(end=timestamp, periods=30, freq="3min")
        closes = pd.Series(range(90, 120), dtype=float)
        entry = pd.DataFrame({
            "date": dates,
            "open": closes - 0.2,
            "high": closes + 2.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": [1000.0] * 30,
        })
        trend = pd.DataFrame({
            "date": [timestamp - pd.Timedelta(minutes=15)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
            "adx": [40.0],
        })
        candidate = Candidate(
            key="test",
            date="2026-08-14",
            symbol="TEST",
            exchange="NSE",
            timestamp=timestamp,
            old_direction="",
            old_entry=119.0,
        )

        with patch(
            "replay_preopen_static_watchlist_day.evaluate_price_action"
        ) as expensive_price_action:
            accepted, reason, detail, direction = technical_decision(
                (entry, trend), candidate
            )

        self.assertFalse(accepted)
        self.assertEqual(reason, "CANDLE:BREAKOUT_VALIDATION_FAILED")
        self.assertEqual(direction, "BUY")
        self.assertTrue(detail["fast_rejection"])
        expensive_price_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
