from datetime import datetime, timezone

from candle_engine import (
    SymbolCandleBuilder,
    combine_5m_into_15m,
    combine_entry_into_15m,
    _interval_start,
)

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def tick(t, price, cum_vol):
    return {"exchange_timestamp": ts(t), "last_price": price, "volume_traded": cum_vol}


# -- _interval_start boundary rule ------------------------------------

check("09:30:00.000 floors to 09:30 (starts the new interval, not the old one)",
      _interval_start(ts("2026-08-05T09:30:00"), 5) == ts("2026-08-05T09:30:00"))
check("09:32:59 floors to 09:30",
      _interval_start(ts("2026-08-05T09:32:59"), 5) == ts("2026-08-05T09:30:00"))
check("09:34:59.999999 still floors to 09:30",
      _interval_start(ts("2026-08-05T09:34:59.999999"), 5) == ts("2026-08-05T09:30:00"))

# -- SymbolCandleBuilder: basic OHLCV -----------------------------------

b = SymbolCandleBuilder("TESTSYM", interval_minutes=5)
r1 = b.add_tick(tick("2026-08-05T09:30:00", 100.0, 1000))
check("First tick of a new candle returns no finalized candle", r1 is None)
b.add_tick(tick("2026-08-05T09:31:00", 102.0, 1500))
b.add_tick(tick("2026-08-05T09:32:00", 99.0, 1800))
r2 = b.add_tick(tick("2026-08-05T09:33:00", 101.0, 2000))
check("Ticks within the same interval never finalize", r2 is None)

# tick that crosses into the next interval finalizes the previous one
r3 = b.add_tick(tick("2026-08-05T09:35:00", 103.0, 2100))
check("Tick crossing interval boundary finalizes exactly one candle", r3 is not None)
check("Finalized candle open == first tick's price", r3["open"] == 100.0)
check("Finalized candle high == max tick price", r3["high"] == 102.0)
check("Finalized candle low == min tick price", r3["low"] == 99.0)
check("Finalized candle close == last tick before boundary", r3["close"] == 101.0)
check("Finalized candle volume == cumulative volume delta over the interval",
      r3["volume"] == 2000 - 1000)
check("Finalized candle date == interval start (09:30), not first-tick time",
      r3["date"] == ts("2026-08-05T09:30:00"))

# The first 09:35 tick's cumulative-volume delta from the final 09:30
# candle tick belongs to the new interval and must not disappear.
b.add_tick(tick("2026-08-05T09:36:00", 104.0, 2300))
r4 = b.add_tick(tick("2026-08-05T09:40:00", 105.0, 2500))
check("Volume baseline carries across candle boundaries without dropping trades",
      r4["volume"] == (2100 - 2000) + (2300 - 2100))

df = b.finalized_df()
check("finalized_df() never includes the still-forming candle", len(df) == 2)

# -- combine_5m_into_15m -------------------------------------------------

legs_complete = [
    {"date": ts("2026-08-05T09:15:00"), "open": 100, "high": 105, "low": 99, "close": 103, "volume": 500},
    {"date": ts("2026-08-05T09:20:00"), "open": 103, "high": 106, "low": 102, "close": 104, "volume": 600},
    {"date": ts("2026-08-05T09:25:00"), "open": 104, "high": 108, "low": 103, "close": 107, "volume": 700},
]
result_15m = combine_5m_into_15m(legs_complete)
check("Three complete 5-min legs combine into exactly one 15-min candle", len(result_15m) == 1)
c = result_15m[0]
check("15-min open == first leg's open", c["open"] == 100)
check("15-min high == max across all legs", c["high"] == 108)
check("15-min low == min across all legs", c["low"] == 99)
check("15-min close == last leg's close", c["close"] == 107)
check("15-min volume == sum of leg volumes", c["volume"] == 500 + 600 + 700)
check("15-min date == bucket start (09:15)", c["date"] == ts("2026-08-05T09:15:00"))

legs_incomplete = legs_complete[:2]  # only 2 of 3 legs present
result_incomplete = combine_5m_into_15m(legs_incomplete)
check("Incomplete group (2 of 3 legs) never emits a partial 15-min candle",
      len(result_incomplete) == 0)

# -- production 3-minute entry legs --------------------------------------

legs_3m = [
    {
        "date": ts(f"2026-08-05T09:{minute:02d}:00"),
        "open": 100 + i,
        "high": 102 + i,
        "low": 99 + i,
        "close": 101 + i,
        "volume": 100 * (i + 1),
    }
    for i, minute in enumerate((15, 18, 21, 24, 27))
]
result_3m = combine_entry_into_15m(legs_3m, 3)
check("Five complete 3-min legs form one 15-min candle", len(result_3m) == 1)
check("3-min-derived 15-min close comes from fifth leg", result_3m[0]["close"] == 105)
check("Four of five 3-min legs fail closed", combine_entry_into_15m(legs_3m[:4], 3) == [])
check(
    "Duplicate/misaligned 3-min legs fail closed",
    combine_entry_into_15m(legs_3m[:4] + [legs_3m[3]], 3) == [],
)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)

# -- ShadowComparator: timezone mismatch bug regression test --------------
# Found during the first real end-of-day review: a naive ws_candle date
# compared against a tz-aware REST date silently returned zero matches
# for every single candle (no crash, no warning). This guards against
# that regressing.

import pandas as pd_test
import sys as sys_test
import json as json_test
import tempfile as tempfile_test
import data_feed as data_feed_test
from candle_engine import ShadowComparator as ShadowComparator_test

_orig_fetch_candles = data_feed_test.fetch_candles

def _fake_fetch_candles_tz_test(kite, token, interval, lookback_days=1):
    rest_dates = pd_test.to_datetime(["2026-08-05 09:30:00", "2026-08-05 09:35:00"]).tz_localize("Asia/Kolkata")
    return pd_test.DataFrame({"date": rest_dates, "open": [99.0, 100.0], "high": [101.0, 102.0],
                               "low": [98.0, 99.0], "close": [100.0, 101.0], "volume": [1000, 1200]})

data_feed_test.fetch_candles = _fake_fetch_candles_tz_test

with tempfile_test.TemporaryDirectory() as tmpdir_test:
    comparator_test = ShadowComparator_test(kite=None, cfg=None, log_dir=tmpdir_test)
    ws_candle_naive = {"date": pd_test.Timestamp("2026-08-05 09:30:00"), "open": 99.0, "high": 101.0,
                        "low": 98.0, "close": 100.0, "volume": 1000}
    comparator_test.compare_5m_candle("TZTEST", "NSE", ws_candle_naive, instrument_token=999)

    log_path_test = comparator_test._log_path()
    with open(log_path_test) as f:
        record_test = json_test.loads(f.readline())

    check("ShadowComparator matches a naive ws_candle date against a tz-aware REST date "
          "(regression test for the silent-zero-match bug found in end-of-day review)",
          record_test["status"] == "compared")
    check("Matched comparison correctly reports within_tolerance for matching values",
          record_test.get("within_tolerance") is True)

data_feed_test.fetch_candles = _orig_fetch_candles

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
