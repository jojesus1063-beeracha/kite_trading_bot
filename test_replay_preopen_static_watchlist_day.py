import unittest

import pandas as pd

import config as cfg
from replay_preopen_static_watchlist_day import configure_replay, session_candidates


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


if __name__ == "__main__":
    unittest.main()
