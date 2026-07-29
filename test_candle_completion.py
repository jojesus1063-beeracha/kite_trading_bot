import pandas as pd
from data_feed import trim_incomplete_candles

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def make_df(dates):
    n = len(dates)
    return pd.DataFrame({
        "date": pd.to_datetime(dates).tz_localize("Asia/Kolkata"),
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.0] * n, "volume": [1000] * n,
    })

now = pd.Timestamp("2026-07-29 10:34:31", tz="Asia/Kolkata")

# Candle just started (10:30-10:45 for 15min interval) -- 4 min in, should be excluded
df1 = make_df(["2026-07-29 10:00:00", "2026-07-29 10:15:00", "2026-07-29 10:30:00"])
result1 = trim_incomplete_candles(df1, interval_minutes=15, buffer_seconds=10, now=now)
check("Still-forming candle (4 min into 15-min interval) is excluded", len(result1) == 2)
check("Correct candles remain after trim", list(result1["date"].dt.strftime("%H:%M")) == ["10:00", "10:15"])

# Candle exactly at completion boundary, no buffer elapsed yet -- excluded
now_at_boundary = pd.Timestamp("2026-07-29 10:30:00", tz="Asia/Kolkata")
df2 = make_df(["2026-07-29 10:15:00"])
result2 = trim_incomplete_candles(df2, interval_minutes=15, buffer_seconds=10, now=now_at_boundary)
check("Candle at exact completion boundary (before buffer) is excluded", len(result2) == 0)

# Candle at completion + buffer exactly -- included (boundary <=)
now_after_buffer = pd.Timestamp("2026-07-29 10:30:10", tz="Asia/Kolkata")
result3 = trim_incomplete_candles(df2, interval_minutes=15, buffer_seconds=10, now=now_after_buffer)
check("Candle at completion+buffer exactly IS included (inclusive boundary)", len(result3) == 1)

# Candle well past completion -- included
now_well_past = pd.Timestamp("2026-07-29 11:00:00", tz="Asia/Kolkata")
result4 = trim_incomplete_candles(df2, interval_minutes=15, buffer_seconds=10, now=now_well_past)
check("Candle well past completion is included", len(result4) == 1)

# Empty dataframe -- fail-safe, no crash
df_empty = pd.DataFrame({"date": pd.Series(dtype="datetime64[ns, Asia/Kolkata]"), "open": [], "high": [], "low": [], "close": [], "volume": []})
result5 = trim_incomplete_candles(df_empty, interval_minutes=15, buffer_seconds=10, now=now)
check("Empty dataframe handled without crash", len(result5) == 0)

# None -- fail-safe
result6 = trim_incomplete_candles(None, interval_minutes=15, buffer_seconds=10, now=now)
check("None input handled without crash", result6 is None)

# 5-minute interval works correctly too (not just 15-min)
df7 = make_df(["2026-07-29 10:20:00", "2026-07-29 10:25:00", "2026-07-29 10:30:00"])
now7 = pd.Timestamp("2026-07-29 10:32:00", tz="Asia/Kolkata")
result7 = trim_incomplete_candles(df7, interval_minutes=5, buffer_seconds=10, now=now7)
check("5-min interval: still-forming 10:30 candle correctly excluded", len(result7) == 2)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
