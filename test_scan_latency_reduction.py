from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pandas as pd

import config as cfg
from api_rate_limiter import ApiRateLimiter
from candle_cache import IncrementalCandleCache
from data_feed import trim_incomplete_candles
from scan_latency import (
    build_entry_timing,
    select_scan_universe,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


# Shared rolling limiter: first three requests are immediate, the fourth
# waits until a new one-second window is available.
clock_value = [0.0]
sleeps = []


def clock():
    return clock_value[0]


def sleeper(seconds):
    sleeps.append(seconds)
    clock_value[0] += seconds


limiter = ApiRateLimiter(
    3,
    1.0,
    clock=clock,
    sleeper=sleeper,
)

for _ in range(4):
    limiter.wait()

check(
    "Historical limiter allows no more than three immediate calls",
    sleeps == [1.0],
)

timezone_frame = pd.DataFrame(
    {
        "date": pd.DatetimeIndex(
            ["2026-08-05 09:15:00"],
            tz="Asia/Kolkata",
        ),
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "volume": [1000],
    }
)

check(
    "Naive VM time is normalised to broker candle timezone",
    len(
        trim_incomplete_candles(
            timezone_frame,
            15,
            now=datetime(2026, 8, 5, 9, 30, 12),
        )
    ) == 1,
)


# The ordered auto-watchlist supplies the daily priority.  Open positions
# remain monitored even when they sit outside the entry shortlist.
watchlist = [f"S{i:02d}" for i in range(1, 81)]
shortlisted, universe, excluded = select_scan_universe(
    watchlist,
    ["S75", "LEGACY"],
    30,
)

check(
    "Detailed entry evaluation is bounded to 30 priorities",
    shortlisted == watchlist[:30],
)
check(
    "Every open position remains in the scan universe",
    universe[-2:] == ["S75", "LEGACY"],
)
check(
    "Non-shortlisted watchlist symbols are reported explicitly",
    excluded == watchlist[30:],
)


def frame(dates):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": [100.0] * len(dates),
            "high": [101.0] * len(dates),
            "low": [99.0] * len(dates),
            "close": [100.0] * len(dates),
            "volume": [1000] * len(dates),
        }
    )


fetch_calls = []
responses = {
    "15minute": [
        frame(["2026-08-05 09:15:00"]),
        frame(
            [
                "2026-08-05 09:15:00",
                "2026-08-05 09:30:00",
            ]
        ),
    ],
    "5minute": [
        frame(["2026-08-05 09:25:00"]),
        frame(
            [
                "2026-08-05 09:25:00",
                "2026-08-05 09:30:00",
            ]
        ),
    ],
}


def fake_fetcher(
    kite,
    token,
    interval,
    **kwargs,
):
    fetch_calls.append(
        {
            "interval": interval,
            **kwargs,
        }
    )
    return responses[interval].pop(0)


cache = IncrementalCandleCache(
    fetcher=fake_fetcher,
)

first_15m = cache.get(
    object(),
    123,
    "15minute",
    now=datetime(2026, 8, 5, 9, 30, 12),
)
cached_15m = cache.get(
    object(),
    123,
    "15minute",
    now=datetime(2026, 8, 5, 9, 35, 12),
)

check(
    "15-minute history is reused between 15-minute closes",
    len(first_15m) == 1
    and len(cached_15m) == 1
    and len([
        call
        for call in fetch_calls
        if call["interval"] == "15minute"
    ]) == 1,
)

advanced_15m = cache.get(
    object(),
    123,
    "15minute",
    now=datetime(2026, 8, 5, 9, 45, 12),
)

check(
    "15-minute history refreshes after the next candle completes",
    len(advanced_15m) == 2,
)

first_5m = cache.get(
    object(),
    456,
    "5minute",
    now=datetime(2026, 8, 5, 9, 30, 12),
    require_advance=True,
)
advanced_5m = cache.get(
    object(),
    456,
    "5minute",
    now=datetime(2026, 8, 5, 9, 35, 12),
    require_advance=True,
)
incremental_5m_call = [
    call
    for call in fetch_calls
    if call["interval"] == "5minute"
][-1]

check(
    "Five-minute cache appends the newly completed candle",
    len(first_5m) == 1
    and len(advanced_5m) == 2,
)
check(
    "Five-minute refresh requests only a short overlapping tail",
    incremental_5m_call["from_date"]
    == datetime(2026, 8, 5, 9, 20),
)


timing = build_entry_timing(
    pd.Timestamp(
        "2026-08-04 11:35:00",
        tz="Asia/Kolkata",
    ),
    "5minute",
    scan_started_at=pd.Timestamp(
        "2026-08-04 11:40:12",
        tz="Asia/Kolkata",
    ),
    order_submitted_at=pd.Timestamp(
        "2026-08-04 11:41:45",
        tz="Asia/Kolkata",
    ),
)

check(
    "Candle close is derived from the candle-start timestamp",
    timing["signal_candle_close"].endswith(
        "11:40:00+05:30"
    ),
)
check(
    "Entry delay records the exact 105-second TITAN example",
    timing["entry_delay_seconds"] == 105.0,
)


main_source = Path("main.py").read_text(
    encoding="utf-8"
)
scan = next(
    node
    for node in ast.parse(main_source).body
    if isinstance(node, ast.FunctionDef)
    and node.name == "run_full_scan"
)
scan_source = ast.get_source_segment(
    main_source,
    scan,
) or ""

check(
    "Fixed half-second sleeps are removed from the entry scan",
    "time.sleep(0.5)" not in scan_source,
)
check(
    "Candidate ranking still happens before entry submission",
    scan_source.index("rank_entry_candidates(")
    < scan_source.index("place_entry_order("),
)
check(
    "The existing fresh-price rejection remains in front of orders",
    scan_source.index("validate_live_price(")
    < scan_source.index("place_entry_order("),
)
check(
    "Scheduler buffer exceeds the broker candle-finalisation buffer",
    cfg.SCAN_BUFFER_SECONDS
    >= cfg.CANDLE_COMPLETION_BUFFER_SECONDS + 2,
)

for path in (
    "main.py",
    "entry_protection.py",
    "trade_log.py",
):
    source = Path(path).read_text(
        encoding="utf-8"
    )

    for field in (
        "signal_candle_close",
        "scan_started_at",
        "order_submitted_at",
        "entry_delay_seconds",
    ):
        check(
            f"{path} persists {field}",
            f'"{field}"' in source,
        )

print("SCAN LATENCY REDUCTION TESTS PASSED")
