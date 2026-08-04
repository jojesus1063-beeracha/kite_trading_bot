from datetime import datetime, timezone

from candle_engine import SymbolCandleBuilder, combine_5m_into_15m, _interval_start

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

df = b.finalized_df()
check("finalized_df() never includes the still-forming candle", len(df) == 1)

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

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
