"""
Tests the exact gating logic wired into main.py's run_full_scan, in
isolation (since run_full_scan itself is thousands of lines and needs a
live Kite connection to exercise end-to-end -- this tests the identical
conditional logic that was inserted, proving its behavior precisely).
"""

import pandas as pd
from datetime import datetime, timedelta

from watchlist_filters import classify_direction_eligibility, format_watchlist_log, NOT_ENABLED, BUY, SELL

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeSignal:
    def __init__(self, direction):
        self.direction = direction


def apply_watchlist_gate(signal, df_15m, cfg):
    """Exact replica of the logic inserted into main.py, lines 475-480."""
    if signal:
        eligibility, elig_detail = classify_direction_eligibility(df_15m, cfg)
        if eligibility not in (NOT_ENABLED, signal.direction):
            signal = None
    return signal


class FakeCfgOff:
    ENABLE_EMA200_WATCHLIST = False


class FakeCfgOn:
    ENABLE_EMA200_WATCHLIST = True
    EMA200_PERIOD = 200


def make_trending_df(n, start_price, drift):
    start = datetime(2026, 8, 1, 9, 15)
    rows = []
    price = start_price
    for i in range(n):
        price += drift
        rows.append({"date": start + timedelta(minutes=15 * i),
                     "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 10000})
    return pd.DataFrame(rows)


# -- Disabled: signal always survives, regardless of direction/data -------

cfg_off = FakeCfgOff()
short_df = make_trending_df(10, 100.0, 0.5)  # far too short for a real EMA200
buy_signal = FakeSignal("BUY")
result = apply_watchlist_gate(buy_signal, short_df, cfg_off)
check("Disabled -> BUY signal survives even with insufficient candle data", result is buy_signal)

sell_signal = FakeSignal("SELL")
result = apply_watchlist_gate(sell_signal, short_df, cfg_off)
check("Disabled -> SELL signal survives even with insufficient candle data", result is sell_signal)

# -- Enabled, clear uptrend: BUY survives, SELL rejected -------------------

cfg_on = FakeCfgOn()
uptrend_df = make_trending_df(220, 100.0, 0.3)

buy_signal2 = FakeSignal("BUY")
result = apply_watchlist_gate(buy_signal2, uptrend_df, cfg_on)
check("Enabled, uptrend, BUY signal -> survives (matches watchlist eligibility)", result is buy_signal2)

sell_signal2 = FakeSignal("SELL")
result = apply_watchlist_gate(sell_signal2, uptrend_df, cfg_on)
check("Enabled, uptrend, SELL signal -> REJECTED (watchlist says BUY-only)", result is None)

# -- Enabled, clear downtrend: SELL survives, BUY rejected -----------------

downtrend_df = make_trending_df(220, 500.0, -0.3)

sell_signal3 = FakeSignal("SELL")
result = apply_watchlist_gate(sell_signal3, downtrend_df, cfg_on)
check("Enabled, downtrend, SELL signal -> survives", result is sell_signal3)

buy_signal3 = FakeSignal("BUY")
result = apply_watchlist_gate(buy_signal3, downtrend_df, cfg_on)
check("Enabled, downtrend, BUY signal -> REJECTED (watchlist says SELL-only)", result is None)

# -- Enabled, but insufficient data (NEITHER) -> both directions rejected --

sig_buy = FakeSignal("BUY")
result = apply_watchlist_gate(sig_buy, short_df, cfg_on)
check("Enabled, insufficient data (NEITHER) -> BUY signal rejected too (fail closed)", result is None)

# -- No signal to begin with -> gate is a no-op, never crashes -------------

result = apply_watchlist_gate(None, uptrend_df, cfg_on)
check("No signal (None) -> gate returns None, no crash", result is None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
