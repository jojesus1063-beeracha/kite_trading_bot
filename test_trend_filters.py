import pandas as pd
from datetime import datetime, timedelta

from trend_filters import evaluate_200ema_filter, PASS, FAIL, NOT_ENABLED, format_rejection_log, dashboard_display

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
    ENABLE_200_EMA_FILTER = True
    EMA200_TIMEFRAME = "15minute"
    EMA200_PERIOD = 200
    EMA200_LOOKBACK = 250
    EMA200_ALLOW_TOUCH = False
    EMA200_MIN_DISTANCE_PCT = 0.10
    EMA200_SLOPE_LOOKBACK = 5


def make_trending_df(n, start_price, drift_per_bar, start=None):
    """Builds a clean, monotonically trending price series -- enough
    bars for a real EMA200 to warm up and show a clear, unambiguous
    slope, rather than a flat/noisy series that could go either way."""
    start = start or datetime(2026, 8, 1, 9, 15)
    rows = []
    price = start_price
    for i in range(n):
        price += drift_per_bar
        rows.append({
            "date": start + timedelta(minutes=15 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 10000,
        })
    return pd.DataFrame(rows)


# -- NOT_ENABLED: disabled filter never touches the data -----------------

cfg_off = FakeCfg()
cfg_off.ENABLE_200_EMA_FILTER = False
status, detail = evaluate_200ema_filter(None, "BUY", cfg_off)
check("Disabled filter returns NOT_ENABLED even with df=None (never touches data)", status == NOT_ENABLED)

# -- Insufficient candles ---------------------------------------------------

cfg = FakeCfg()
short_df = make_trending_df(50, 100.0, 0.5)
status, detail = evaluate_200ema_filter(short_df, "BUY", cfg)
check("Insufficient candles (50 < 200+5) -> FAIL, not a crash", status == FAIL)
check("Insufficient-candles reason mentions the shortfall", "insufficient" in detail["reason"].lower())

# -- Missing/empty data -----------------------------------------------------

status, detail = evaluate_200ema_filter(pd.DataFrame(), "BUY", cfg)
check("Empty DataFrame -> FAIL, not a crash", status == FAIL)

status, detail = evaluate_200ema_filter(None, "BUY", cfg)
check("None DataFrame (filter enabled) -> FAIL, not a crash", status == FAIL)

# -- Clear uptrend: price well above EMA200, rising slope ------------------

uptrend_df = make_trending_df(260, 100.0, 0.3)
status, detail = evaluate_200ema_filter(uptrend_df, "BUY", cfg)
check("Clear uptrend, BUY direction -> PASS", status == PASS)
check("PASS detail includes ema200/close/slope/distance", all(k in detail for k in ("ema200", "close", "slope", "distance_pct")))
check("Slope correctly labeled positive for a rising series", detail["slope"] == "positive")

status, detail = evaluate_200ema_filter(uptrend_df, "SELL", cfg)
check("Clear uptrend, but SELL direction requested -> FAIL (counter-trend rejected)", status == FAIL)

# -- Clear downtrend ---------------------------------------------------------

downtrend_df = make_trending_df(260, 500.0, -0.3)
status, detail = evaluate_200ema_filter(downtrend_df, "SELL", cfg)
check("Clear downtrend, SELL direction -> PASS", status == PASS)
check("Slope correctly labeled negative for a falling series", detail["slope"] == "negative")

status, detail = evaluate_200ema_filter(downtrend_df, "BUY", cfg)
check("Clear downtrend, but BUY direction requested -> FAIL (counter-trend rejected)", status == FAIL)

# -- Flat EMA (no real slope either way) ------------------------------------

flat_df = make_trending_df(260, 100.0, 0.0)
status, detail = evaluate_200ema_filter(flat_df, "BUY", cfg)
check("Flat EMA (zero drift) -> FAIL for BUY (slope not positive)", status == FAIL)
check("Flat EMA correctly labeled 'flat', not positive/negative", detail.get("slope") == "flat")

# -- Touching the EMA (within min distance) ---------------------------------

# Build an uptrend, then flatten the last few bars right at the EMA level
touch_df = make_trending_df(260, 100.0, 0.3)
last_ema_estimate = touch_df["close"].iloc[-1]  # close enough for a touch test
touch_df.loc[touch_df.index[-1], "close"] = last_ema_estimate  # still near current EMA
cfg_strict = FakeCfg()
cfg_strict.EMA200_MIN_DISTANCE_PCT = 50.0  # deliberately huge, forces a "touching" classification for this test
status, detail = evaluate_200ema_filter(touch_df, "BUY", cfg_strict)
check("Price within the configured minimum distance -> FAIL (touching, not confirmed)", status == FAIL)
check("Touching case reason mentions distance/touching", "touch" in detail["reason"].lower() or "distance" in detail["reason"].lower())

# -- EMA200_ALLOW_TOUCH=True bypasses the distance check --------------------

cfg_allow_touch = FakeCfg()
cfg_allow_touch.EMA200_MIN_DISTANCE_PCT = 50.0
cfg_allow_touch.EMA200_ALLOW_TOUCH = True
status, detail = evaluate_200ema_filter(uptrend_df, "BUY", cfg_allow_touch)
check("EMA200_ALLOW_TOUCH=True bypasses the distance gate (still evaluates trend/slope)", status == PASS)

# -- Cache reuse: this function must not fetch anything itself -------------

import trend_filters
has_fetch_import = "fetch_candles" in open(trend_filters.__file__).read() or "kite." in open(trend_filters.__file__).read()
check("Module never imports/calls anything that fetches data -- purely a function of the df_15m passed in",
      not has_fetch_import)

# -- Never raises, even on malformed input ----------------------------------

malformed_df = pd.DataFrame({"close": [1, 2, 3]})  # missing 'date', 'open', etc.
try:
    status, detail = evaluate_200ema_filter(malformed_df, "BUY", cfg)
    check("Malformed DataFrame (missing columns) -> handled gracefully, no exception propagates", status == FAIL)
except Exception as e:
    check(f"Malformed DataFrame should never raise, but got: {e}", False)

# -- format_rejection_log and dashboard_display don't crash on any status --

for test_status, test_detail in [
    (NOT_ENABLED, {"reason": "filter disabled"}),
    (FAIL, {"reason": "test", "direction": "BUY", "ema200": 100.0, "close": 99.0, "slope": "flat", "distance_pct": -1.0}),
    (PASS, {"reason": "test", "direction": "SELL", "ema200": 100.0, "close": 95.0, "slope": "negative", "distance_pct": -5.0}),
]:
    log_line = format_rejection_log("TESTSYM", test_status, test_detail)
    check(f"format_rejection_log produces a string for status={test_status}", isinstance(log_line, str) and len(log_line) > 0)
    dash = dashboard_display(test_status, test_detail)
    check(f"dashboard_display produces a dict for status={test_status}", isinstance(dash, dict) and "filter" in dash)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
