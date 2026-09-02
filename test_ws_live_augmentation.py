from datetime import datetime, timezone, timedelta

import pandas as pd

from ws_integration import WSShadowEngine

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeKite:
    def historical_data(self, *a, **kw):
        return []


def make_rest_df(last_date, n=5, interval_minutes=3, tz=None):
    rows = []
    for i in range(n):
        d = last_date - timedelta(minutes=interval_minutes * (n - 1 - i))
        if tz is not None:
            d = pd.Timestamp(d).tz_localize(tz) if pd.Timestamp(d).tzinfo is None else pd.Timestamp(d)
        rows.append({"date": d, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000})
    return pd.DataFrame(rows)


engine = WSShadowEngine(FakeKite(), symbols=["SYM"], tokens={"SYM": 1}, exchange_map={"SYM": "NSE"})

# -- Case 1: no WS candle available at all -> unchanged, not augmented ----

last_date = datetime(2026, 8, 5, 9, 30)
df = make_rest_df(last_date)
result_df, augmented = engine.get_augmented_candles("SYM", "3minute", df)
check("No WS candle available -> df returned unmodified", result_df.equals(df))
check("No WS candle available -> augmented=False", augmented is False)

# -- Case 2: WS candle is exactly the next interval -> gets appended -----

engine.candle_builders_entry["SYM"].finalized.append({
    "date": last_date + timedelta(minutes=3),
    "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1200,
})
engine._validated_entry_dates["SYM"].add(
    engine._candle_key(last_date + timedelta(minutes=3))
)
# freeze "now" perception by using a date close to the WS candle's date
import ws_integration as wi
orig_now = pd.Timestamp.now
pd.Timestamp.now = staticmethod(lambda tz=None: pd.Timestamp(last_date + timedelta(minutes=3, seconds=10), tz=tz))

result_df, augmented = engine.get_augmented_candles("SYM", "3minute", df)
check("Exact next-interval WS candle -> augmented=True", augmented is True)
check("Augmented df has one more row than original", len(result_df) == len(df) + 1)
check("Appended row's close matches the WS candle", result_df.iloc[-1]["close"] == 101.5)
check("Original df object itself is untouched (no in-place mutation)", len(df) == 5)

pd.Timestamp.now = orig_now

# -- Case 3: WS candle skips ahead (gap) -> NOT augmented, stays safe ----

engine2 = WSShadowEngine(FakeKite(), symbols=["SYM2"], tokens={"SYM2": 1}, exchange_map={"SYM2": "NSE"})
engine2.candle_builders_entry["SYM2"].finalized.append({
    "date": last_date + timedelta(minutes=6),  # skipped a candle -- gap
    "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1200,
})
engine2._validated_entry_dates["SYM2"].add(
    engine2._candle_key(last_date + timedelta(minutes=6))
)
df2 = make_rest_df(last_date)
result_df2, augmented2 = engine2.get_augmented_candles("SYM2", "3minute", df2)
check("Gapped WS candle (skips ahead) -> NOT augmented", augmented2 is False)
check("Gapped case -> df returned unmodified", result_df2.equals(df2))

# -- Case 4: WS candle is stale (too old relative to 'now') -> NOT augmented

engine3 = WSShadowEngine(FakeKite(), symbols=["SYM3"], tokens={"SYM3": 1}, exchange_map={"SYM3": "NSE"})
engine3.candle_builders_entry["SYM3"].finalized.append({
    "date": last_date + timedelta(minutes=3),
    "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1200,
})
engine3._validated_entry_dates["SYM3"].add(
    engine3._candle_key(last_date + timedelta(minutes=3))
)
df3 = make_rest_df(last_date)
# "now" is far in the future relative to the WS candle -> stale
pd.Timestamp.now = staticmethod(lambda tz=None: pd.Timestamp(last_date + timedelta(hours=5), tz=tz))
result_df3, augmented3 = engine3.get_augmented_candles("SYM3", "3minute", df3)
pd.Timestamp.now = orig_now
check("Stale WS candle (too old vs now) -> NOT augmented", augmented3 is False)

# -- Case 5: empty REST df -> unmodified, no crash -------------------------

empty_df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
result_df4, augmented4 = engine.get_augmented_candles("SYM", "3minute", empty_df)
check("Empty REST df -> returned unmodified, no crash", augmented4 is False)

# -- Case 6: unknown symbol -> unmodified, no crash ------------------------

result_df5, augmented5 = engine.get_augmented_candles("NOT_A_SYMBOL", "3minute", df)
check("Unknown symbol -> not augmented, no crash", augmented5 is False)

# -- Case 7: 15-minute timeframe path --------------------------------------

engine4 = WSShadowEngine(FakeKite(), symbols=["SYM4"], tokens={"SYM4": 1}, exchange_map={"SYM4": "NSE"})
last_date_15 = datetime(2026, 8, 5, 9, 15)
df15 = make_rest_df(last_date_15, n=3, interval_minutes=15)
engine4._last_finalized_15m["SYM4"] = {
    "date": last_date_15 + timedelta(minutes=15),
    "open": 100, "high": 103, "low": 99, "close": 102, "volume": 3000,
}
pd.Timestamp.now = staticmethod(lambda tz=None: pd.Timestamp(last_date_15 + timedelta(minutes=15, seconds=5), tz=tz))
result_df6, augmented6 = engine4.get_augmented_candles("SYM4", "15minute", df15)
pd.Timestamp.now = orig_now
check("15-minute timeframe: exact next interval -> augmented=True", augmented6 is True)
check("15-minute augmented df has one more row", len(result_df6) == len(df15) + 1)

# -- Case 8: timezone-aware REST df, naive WS candle -> normalizes cleanly -

engine5 = WSShadowEngine(FakeKite(), symbols=["SYM5"], tokens={"SYM5": 1}, exchange_map={"SYM5": "NSE"})
tz_ist = "Asia/Kolkata"
last_date_tz = pd.Timestamp(datetime(2026, 8, 5, 9, 30)).tz_localize(tz_ist)
df_tz = pd.DataFrame([{
    "date": last_date_tz, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000,
}])
engine5.candle_builders_entry["SYM5"].finalized.append({
    "date": datetime(2026, 8, 5, 9, 33),  # naive -- as Kite ticks actually arrive
    "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1200,
})
engine5._validated_entry_dates["SYM5"].add(
    engine5._candle_key(datetime(2026, 8, 5, 9, 33))
)
pd.Timestamp.now = staticmethod(lambda tz=None: pd.Timestamp(datetime(2026, 8, 5, 9, 33, 5), tz=tz))
result_df7, augmented7 = engine5.get_augmented_candles("SYM5", "3minute", df_tz)
pd.Timestamp.now = orig_now
check("tz-aware REST df + naive WS candle -> normalizes and augments", augmented7 is True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
