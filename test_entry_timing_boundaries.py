"""
Sections 11 & 12: per-filter boundary behavior and filter combinations.

Section 11 explicitly requires verifying the ACTUAL comparison
operators rather than assuming equality behavior. These tests pin down
exactly what happens AT each threshold, not just above/below.
"""

import pandas as pd
from datetime import datetime, timedelta

from entry_timing import evaluate_entry_timing, INVALID, OPTIMAL, ACCEPTABLE, LATE

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class Cfg:
    ENABLE_ENTRY_TIMING_FILTER = True
    MAX_ENTRY_EXTENSION_ATR = 1.50
    ATR_PERIOD = 14
    ENABLE_CONFIRMATION_QUALITY_FILTER = True
    MIN_CONFIRMATION_BODY_RATIO = 0.50
    ENABLE_VOLUME_ACCELERATION_FILTER = True
    MIN_CONFIRMATION_VOLUME_ACCELERATION = 1.10


def make_df(n=20, price=100.0, tr=2.0):
    """Constant true range -> ATR converges to exactly tr."""
    base = datetime(2026, 8, 10, 9, 15)
    return pd.DataFrame([{
        "date": base + timedelta(minutes=5 * i),
        "open": price, "high": price + tr / 2, "low": price - tr / 2,
        "close": price, "ema_entry": price, "avg_volume": 1000, "volume": 1000,
    } for i in range(n)])


def candle(open_, high, low, close, ema_entry, volume):
    return pd.Series({
        "date": datetime(2026, 8, 10, 10, 0), "open": open_, "high": high,
        "low": low, "close": close, "ema_entry": ema_entry,
        "avg_volume": 1000, "volume": volume,
    })


df = make_df(n=20, price=100.0, tr=2.0)  # ATR = 2.0
prev = candle(99.5, 100.5, 99.0, 100.0, 100.0, 1000)


# ============ ANTI-CHASE BOUNDARY (ATR = 2.0, threshold 1.5) ============
# extension_atr = (close - ema_entry) / atr

# Below: close 102.0 -> (102-100)/2 = 1.0 ATR
c = candle(101.0, 102.2, 100.9, 102.0, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("ANTI-CHASE below threshold (1.0 < 1.5) -> not blocked by extension",
      "ENTRY_EXTENSION_TOO_HIGH" not in d.get("blocking_reasons", []))

# Exactly AT: close 103.0 -> (103-100)/2 = 1.5 ATR exactly
c = candle(102.0, 103.2, 101.9, 103.0, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("ANTI-CHASE EXACTLY at threshold (1.5 == 1.5) -> ALLOWED "
      "(operator is `>` not `>=`, verified from behavior)",
      "ENTRY_EXTENSION_TOO_HIGH" not in d.get("blocking_reasons", []))
check("ANTI-CHASE at-threshold extension value is exactly 1.5", abs(d["extension_atr"] - 1.5) < 1e-9)

# Above: close 104.0 -> 2.0 ATR
c = candle(103.0, 104.2, 102.9, 104.0, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("ANTI-CHASE above threshold (2.0 > 1.5) -> BLOCKED",
      "ENTRY_EXTENSION_TOO_HIGH" in d.get("blocking_reasons", []))

# ATR unavailable (too few bars) -> must not block
cl, d = evaluate_entry_timing("T", "BUY", make_df(n=3), c, prev, Cfg())
check("ANTI-CHASE with ATR unavailable -> does NOT block (fail-open on this filter only)",
      "ENTRY_EXTENSION_TOO_HIGH" not in d.get("blocking_reasons", []))


# ============ CONFIRMATION BODY BOUNDARY (threshold 0.50) ============
# body_to_range = |close-open| / (high-low)

# Exactly AT: body 1.0, range 2.0 -> 0.50
c = candle(100.5, 101.5, 99.5, 101.5, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("CONFIRMATION body EXACTLY at threshold (0.50 == 0.50) -> ALLOWED "
      "(operator is `>=`, verified from behavior)",
      "CONFIRMATION_BODY_TOO_WEAK" not in d.get("blocking_reasons", []))
check("CONFIRMATION at-threshold body ratio is exactly 0.5", abs(d["body_to_range_ratio"] - 0.5) < 1e-9)

# Below: body 0.4, range 2.0 -> 0.20
c = candle(100.9, 101.5, 99.5, 101.3, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("CONFIRMATION body below threshold -> BLOCKED",
      "CONFIRMATION_BODY_TOO_WEAK" in d.get("blocking_reasons", []))

# Wrong direction: bearish candle on a BUY
c = candle(101.5, 101.6, 100.4, 100.5, 100.0, 2000)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("CONFIRMATION bearish candle on a BUY -> BLOCKED regardless of body size",
      "CONFIRMATION_BODY_TOO_WEAK" in d.get("blocking_reasons", []))

# Zero range (high == low) -> ratio 0.0, must not crash
c = candle(101.0, 101.0, 101.0, 101.0, 100.0, 2000)
try:
    cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
    check("CONFIRMATION zero-range candle -> no crash, ratio 0.0",
          d.get("body_to_range_ratio") == 0.0)
except ZeroDivisionError:
    check("CONFIRMATION zero-range must not raise ZeroDivisionError", False)


# ============ VOLUME ACCELERATION BOUNDARY (threshold 1.10) ============
# acceleration = curr.volume / prev.volume ; prev.volume = 1000

good_body = dict(open_=100.5, high=101.5, low=99.5, close=101.5, ema_entry=100.0)

# Exactly AT: 1100 / 1000 = 1.10
c = candle(volume=1100, **good_body)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("VOLUME acceleration EXACTLY at threshold (1.10 == 1.10) -> ALLOWED "
      "(operator is `>=`, verified from behavior)",
      "VOLUME_ACCELERATION_TOO_LOW" not in d.get("blocking_reasons", []))

# Below: 1000 / 1000 = 1.0
c = candle(volume=1000, **good_body)
cl, d = evaluate_entry_timing("T", "BUY", df, c, prev, Cfg())
check("VOLUME acceleration below threshold (1.0 < 1.10) -> BLOCKED",
      "VOLUME_ACCELERATION_TOO_LOW" in d.get("blocking_reasons", []))

# Zero previous volume -> must not divide by zero
prev_zero = candle(99.5, 100.5, 99.0, 100.0, 100.0, 0)
c = candle(volume=2000, **good_body)
try:
    cl, d = evaluate_entry_timing("T", "BUY", df, c, prev_zero, Cfg())
    check("VOLUME zero previous volume -> no ZeroDivisionError; blocks (fails closed)",
          "VOLUME_ACCELERATION_TOO_LOW" in d.get("blocking_reasons", []))
except ZeroDivisionError:
    check("VOLUME zero previous volume must not raise ZeroDivisionError", False)


# ============ SECTION 12: COMBINATIONS ============

all_pass = candle(volume=2000, **good_body)  # ext 1.5? -> close 101.5 => 0.75 ATR, body 0.5, vol 2.0
cl, d = evaluate_entry_timing("T", "BUY", df, all_pass, prev, Cfg())
check("COMBO all three PASS -> not INVALID", cl != INVALID)

only_anti_fails = candle(103.0, 104.2, 102.9, 104.0, 100.0, 2000)  # ext 2.0, body ~0.85, vol 2.0
cl, d = evaluate_entry_timing("T", "BUY", df, only_anti_fails, prev, Cfg())
check("COMBO only anti-chase FAILS -> INVALID, blocked solely by ENTRY_EXTENSION_TOO_HIGH",
      cl == INVALID and d["blocking_reasons"] == ["ENTRY_EXTENSION_TOO_HIGH"])

only_conf_fails = candle(100.9, 101.5, 99.5, 101.3, 100.0, 2000)  # body 0.2
cl, d = evaluate_entry_timing("T", "BUY", df, only_conf_fails, prev, Cfg())
check("COMBO only confirmation FAILS -> blocked solely by CONFIRMATION_BODY_TOO_WEAK",
      cl == INVALID and d["blocking_reasons"] == ["CONFIRMATION_BODY_TOO_WEAK"])

only_vol_fails = candle(volume=1000, **good_body)
cl, d = evaluate_entry_timing("T", "BUY", df, only_vol_fails, prev, Cfg())
check("COMBO only volume FAILS -> blocked solely by VOLUME_ACCELERATION_TOO_LOW",
      cl == INVALID and d["blocking_reasons"] == ["VOLUME_ACCELERATION_TOO_LOW"])

all_fail = candle(103.9, 104.5, 102.5, 104.0, 100.0, 500)  # ext 2.0, weak body, vol 0.5
cl, d = evaluate_entry_timing("T", "BUY", df, all_fail, prev, Cfg())
check("COMBO all three FAIL -> all three reasons recorded deterministically, in fixed order",
      cl == INVALID and len(d["blocking_reasons"]) == 3
      and d["blocking_reasons"] == ["ENTRY_EXTENSION_TOO_HIGH", "CONFIRMATION_BODY_TOO_WEAK",
                                     "VOLUME_ACCELERATION_TOO_LOW"])

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
