from datetime import date
import pandas as pd

from download_nifty500_history import chunks, completeness, merge_frames, requested_subranges


def test_chunks_cover_range_without_overlap():
    got = list(chunks(date(2026, 1, 1), date(2026, 3, 5), 30))
    assert got == [
        (date(2026, 1, 1), date(2026, 1, 30)),
        (date(2026, 1, 31), date(2026, 3, 1)),
        (date(2026, 3, 2), date(2026, 3, 5)),
    ]


def test_merge_deduplicates_timestamp_keep_latest():
    a = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-08-20 09:15", "2026-08-20 09:18"]).tz_localize("Asia/Kolkata"),
        "close": [100.0, 101.0],
    })
    b = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-08-20 09:18", "2026-08-20 09:21"]).tz_localize("Asia/Kolkata"),
        "close": [101.5, 102.0],
    })
    got = merge_frames(a, [b])
    assert len(got) == 3
    assert float(got.loc[got["timestamp"].dt.minute.eq(18), "close"].iloc[0]) == 101.5


def test_requested_subranges_resume_edges_only():
    existing = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-02-01 09:15", "2026-04-30 15:27"]).tz_localize("Asia/Kolkata")
    })
    got = requested_subranges(existing, date(2026, 1, 1), date(2026, 5, 31), False)
    assert got == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 5, 1), date(2026, 5, 31)),
    ]


def test_completeness_flags_large_same_day_gap():
    df = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-08-20 09:15",
            "2026-08-20 09:18",
            "2026-08-20 09:30",
        ]).tz_localize("Asia/Kolkata")
    })
    got = completeness(df, date(2026, 8, 20), date(2026, 8, 20))
    assert got["rows"] == 3
    assert got["trading_dates"] == 1
    assert got["large_intraday_gaps"] == 1
