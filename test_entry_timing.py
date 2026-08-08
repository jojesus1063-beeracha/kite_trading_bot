import pandas as pd
from datetime import datetime, timedelta

from entry_timing import (
    evaluate_entry_timing, format_entry_timing_log, _local_atr, _confirmation_quality,
    OPTIMAL, ACCEPTABLE, LATE, INVALID, NOT_ENABLED,
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


class FakeCfg:
    ENABLE_ENTRY_TIMING_FILTER = True
    MAX_ENTRY_EXTENSION_ATR = 1.50
    ATR_PERIOD = 14
    ENABLE_CONFIRMATION_QUALITY_FILTER = True
    MIN_CONFIRMATION_BODY_RATIO = 0.50
    ENABLE_VOLUME_ACCELERATION_FILTER = False
    MIN_CONFIRMATION_VOLUME_ACCELERATION = 1.10


def make_5m(n=20, base_price=100.0, true_range=1.0):
    """n candles with a controlled, constant true range so ATR is predictable."""
    base = datetime(2026, 8, 10, 9, 15)
    rows = []
    for i in range(n):
        rows.append({
            "date": base + timedelta(minutes=5 * i),
            "open": base_price, "high": base_price + true_range / 2,
            "low": base_price - true_range / 2, "close": base_price,
            "ema_entry": base_price, "avg_volume": 1000, "volume": 1000,
        })
    return pd.DataFrame(rows)


def make_candle(open_, high, low, close, ema_entry, volume=2000):
    return pd.Series({
        "date": datetime(2026, 8, 10, 10, 0), "open": open_, "high": high,
        "low": low, "close": close, "ema_entry": ema_entry,
        "avg_volume": 1000, "volume": volume,
    })


# -- Disabled -> never blocks, never inspects ----------------------------

cfg_off = FakeCfg()
cfg_off.ENABLE_ENTRY_TIMING_FILTER = False
classification, detail = evaluate_entry_timing("TEST", "BUY", None, None, None, cfg_off)
check("Filter disabled -> NOT_ENABLED, never touches data", classification == NOT_ENABLED)


# -- ATR computed locally from df_5m (which has NO atr column) ------------

df = make_5m(n=20, base_price=100.0, true_range=2.0)
check("df_5m genuinely has no 'atr' column, matching real add_indicators() output",
      "atr" not in df.columns)
atr_value = _local_atr(df, 14)
check("_local_atr computes ~2.0 for a constant 2.0 true range", atr_value is not None and abs(atr_value - 2.0) < 0.01)
check("_local_atr returns None with insufficient candles", _local_atr(make_5m(n=5), 14) is None)
check("_local_atr returns None on missing OHLC columns",
      _local_atr(pd.DataFrame({"close": [1, 2, 3]}), 14) is None)


# -- Anti-chase: within limit -> not blocked ------------------------------

cfg = FakeCfg()
# ATR=2.0, ema_entry=100, entry=101 -> extension = 0.5 ATR (well within 1.5)
prev = make_candle(99.5, 100.5, 99.0, 100.0, 100.0, volume=1000)
curr_ok = make_candle(100.2, 101.2, 100.0, 101.0, 100.0, volume=2000)
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_ok, prev, cfg)
check("BUY within extension limit -> not INVALID", classification != INVALID)
check("extension_atr correctly computed as 0.5", abs(detail["extension_atr"] - 0.5) < 0.01)


# -- Anti-chase: beyond limit -> INVALID ----------------------------------

# ATR=2.0, ema_entry=100, entry=105 -> extension = 2.5 ATR (exceeds 1.5)
curr_extended = make_candle(104.0, 105.5, 103.8, 105.0, 100.0, volume=2000)
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_extended, prev, cfg)
check("BUY beyond extension limit -> INVALID", classification == INVALID)
check("Blocking reason is ENTRY_EXTENSION_TOO_HIGH",
      "ENTRY_EXTENSION_TOO_HIGH" in detail["blocking_reasons"])

# SELL mirror: ema_entry=100, entry=95 -> extension = 2.5 ATR
curr_extended_sell = make_candle(96.0, 96.2, 94.5, 95.0, 100.0, volume=2000)
classification, detail = evaluate_entry_timing("TEST", "SELL", df, curr_extended_sell, prev, cfg)
check("SELL beyond extension limit -> INVALID (mirrored logic)", classification == INVALID)


# -- Confirmation candle quality ------------------------------------------

quality = _confirmation_quality(make_candle(100.0, 102.0, 99.0, 101.5, 100.0))
check("_confirmation_quality computes body/range correctly",
      abs(quality["body_to_range_ratio"] - (1.5 / 3.0)) < 0.001)

# Weak body: large range, tiny body, filter ENABLED -> INVALID
cfg_q = FakeCfg()
curr_weak = make_candle(100.9, 103.0, 99.0, 101.0, 100.0, volume=2000)  # body 0.1, range 4.0
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_weak, prev, cfg_q)
check("Weak confirmation body with filter ENABLED -> INVALID",
      classification == INVALID and "CONFIRMATION_BODY_TOO_WEAK" in detail["blocking_reasons"])

# Same weak candle, filter DISABLED -> must NOT block
cfg_q_off = FakeCfg()
cfg_q_off.ENABLE_CONFIRMATION_QUALITY_FILTER = False
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_weak, prev, cfg_q_off)
check("Same weak body with quality filter DISABLED -> not blocked (measured only)",
      classification != INVALID)
check("Body ratio still RECORDED even when the filter is disabled",
      detail["body_to_range_ratio"] is not None)


# -- Volume acceleration (default OFF per spec) ---------------------------

cfg_v = FakeCfg()  # ENABLE_VOLUME_ACCELERATION_FILTER = False by default
curr_low_vol = make_candle(100.2, 101.2, 100.0, 101.0, 100.0, volume=900)  # accel 0.9 < 1.10
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_low_vol, prev, cfg_v)
check("Low volume acceleration with filter DEFAULT OFF -> not blocked",
      classification != INVALID)
check("volume_acceleration still measured and recorded when filter is off",
      abs(detail["volume_acceleration"] - 0.9) < 0.01)

cfg_v_on = FakeCfg()
cfg_v_on.ENABLE_VOLUME_ACCELERATION_FILTER = True
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_low_vol, prev, cfg_v_on)
check("Low volume acceleration with filter ENABLED -> INVALID",
      classification == INVALID and "VOLUME_ACCELERATION_TOO_LOW" in detail["blocking_reasons"])


# -- Classification grading (non-blocking) --------------------------------

cfg_grade = FakeCfg()
cfg_grade.ENABLE_CONFIRMATION_QUALITY_FILTER = False
# extension 0.25 ATR (<= 0.75 = 50% of 1.5), strong body -> OPTIMAL
curr_optimal = make_candle(100.0, 100.6, 99.9, 100.5, 100.0, volume=2000)
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_optimal, prev, cfg_grade)
check("Small extension + strong body -> OPTIMAL", classification == OPTIMAL)

# extension 1.4 ATR -> between 80% and 100% of limit -> LATE (but NOT blocked)
curr_late = make_candle(102.5, 102.9, 102.4, 102.8, 100.0, volume=2000)
classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_late, prev, cfg_grade)
check("Extension near but under the limit -> LATE, and NOT blocked",
      classification == LATE)


# -- Fail-safe paths -------------------------------------------------------

cfg_safe = FakeCfg()
short_df = make_5m(n=3)  # too few candles for ATR
classification, detail = evaluate_entry_timing("TEST", "BUY", short_df, curr_ok, prev, cfg_safe)
check("Insufficient candles for ATR -> ACCEPTABLE (non-blocking), not a crash",
      classification == ACCEPTABLE)

try:
    classification, detail = evaluate_entry_timing("TEST", "BUY", df, curr_ok, None, cfg_safe)
    check("prev=None -> handled without crash", classification != INVALID or True)
except Exception as e:
    check(f"Should never raise on prev=None, but got: {e}", False)

malformed = pd.Series({"date": datetime(2026, 8, 10, 10, 0), "close": 100.0})
try:
    classification, detail = evaluate_entry_timing("TEST", "BUY", df, malformed, prev, cfg_safe)
    check("Malformed candle -> degrades to non-blocking ACCEPTABLE, never silently kills a signal",
          classification == ACCEPTABLE)
except Exception as e:
    check(f"Should never raise on malformed input, but got: {e}", False)


# -- Logging never crashes -------------------------------------------------

for c, d in [(NOT_ENABLED, {}), (OPTIMAL, {"direction": "BUY", "extension_atr": 0.3}),
             (INVALID, {"direction": "BUY", "blocking_reasons": ["ENTRY_EXTENSION_TOO_HIGH"]})]:
    line = format_entry_timing_log("TEST", c, d)
    check(f"format_entry_timing_log works for {c}", isinstance(line, str) and len(line) > 0)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
