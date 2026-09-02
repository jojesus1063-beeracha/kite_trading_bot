import pandas as pd
from datetime import datetime, timedelta

from watchlist_filters import classify_direction_eligibility, BUY, SELL, NEITHER, NOT_ENABLED, format_watchlist_log

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
    ENABLE_EMA200_WATCHLIST = True
    EMA200_PERIOD = 200


def make_trending_df(n, start_price, drift_per_bar):
    start = datetime(2026, 8, 1, 9, 15)
    rows = []
    price = start_price
    for i in range(n):
        price += drift_per_bar
        rows.append({
            "date": start + timedelta(minutes=15 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 10000,
        })
    return pd.DataFrame(rows)


# -- Disabled: never touches data --------------------------------------

cfg_off = FakeCfg()
cfg_off.ENABLE_EMA200_WATCHLIST = False
result, detail = classify_direction_eligibility(None, cfg_off)
check("Disabled -> NOT_ENABLED even with df=None (never touches data)", result == NOT_ENABLED)

# -- Insufficient data ----------------------------------------------------

cfg = FakeCfg()
short_df = make_trending_df(50, 100.0, 0.5)
result, detail = classify_direction_eligibility(short_df, cfg)
check("Insufficient candles -> NEITHER, not a crash", result == NEITHER)

result, detail = classify_direction_eligibility(pd.DataFrame(), cfg)
check("Empty DataFrame -> NEITHER, not a crash", result == NEITHER)

result, detail = classify_direction_eligibility(None, cfg)
check("None (enabled) -> NEITHER, not a crash", result == NEITHER)

# -- Clear uptrend -> BUY eligible, SELL not ------------------------------

uptrend_df = make_trending_df(220, 100.0, 0.3)
result, detail = classify_direction_eligibility(uptrend_df, cfg)
check("Clear uptrend -> classified BUY", result == BUY)
check("BUY detail includes ema200/close", "ema200" in detail and "close" in detail)

# -- Clear downtrend -> SELL eligible --------------------------------------

downtrend_df = make_trending_df(220, 500.0, -0.3)
result, detail = classify_direction_eligibility(downtrend_df, cfg)
check("Clear downtrend -> classified SELL", result == SELL)

# -- Never raises on malformed input ---------------------------------------

malformed_df = pd.DataFrame({"close": [1, 2, 3]})
try:
    result, detail = classify_direction_eligibility(malformed_df, cfg)
    check("Malformed DataFrame -> handled gracefully (NEITHER), no exception", result == NEITHER)
except Exception as e:
    check(f"Should never raise, but got: {e}", False)

# -- Logging never crashes on any status -----------------------------------

for s, d in [(NOT_ENABLED, {}), (BUY, {"ema200": 100, "close": 105, "reason": "x"}),
             (SELL, {"ema200": 100, "close": 95, "reason": "x"}), (NEITHER, {"reason": "x"})]:
    line = format_watchlist_log("TEST", s, d)
    check(f"format_watchlist_log works for status={s}", isinstance(line, str) and len(line) > 0)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
