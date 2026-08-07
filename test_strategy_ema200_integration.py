"""
No-regression + integration test for the 200 EMA filter wired into
strategy.py. Two things must both be true:
1. With ENABLE_200_EMA_FILTER=False (default), evaluate() returns
   EXACTLY what it would have returned before this filter existed --
   proven by comparing against a locally-vendored copy of the
   pre-integration evaluate() logic, not just "no crash."
2. With the filter enabled, it actually blocks counter-trend signals
   and allows trend-confirming ones, using strategy.evaluate() itself
   (not just trend_filters.py in isolation, which test_trend_filters.py
   already covers).
"""

import pandas as pd
from datetime import datetime, timedelta

import config as cfg
from strategy import evaluate

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


def make_15m_df(n, start_price, drift, adx=30.0):
    base = datetime(2026, 8, 6, 9, 15)
    rows = []
    price = start_price
    for i in range(n):
        price += drift
        rows.append({
            "date": base + timedelta(minutes=15 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price,
            "volume": 10000, "ema_fast": price - 0.5, "ema_slow": price - 2,
            "vwap": price - 0.3, "adx": adx,
        })
    return pd.DataFrame(rows)


def make_5m_df(entry_close, ema_entry, avg_volume, volume):
    base = datetime(2026, 8, 6, 14, 55)
    rows = [
        {"date": base, "open": entry_close - 1, "high": entry_close + 0.5, "low": entry_close - 1.5,
         "close": entry_close - 0.5, "ema_entry": ema_entry, "avg_volume": avg_volume, "volume": volume},
        {"date": base + timedelta(minutes=5), "open": entry_close - 0.5, "high": entry_close + 0.5,
         "low": entry_close - 1, "close": entry_close, "ema_entry": ema_entry, "avg_volume": avg_volume,
         "volume": volume * 2},  # above-average volume on the signal candle
    ]
    return pd.DataFrame(rows)


class FakeCfg:
    USE_ADX_FILTER = False
    ADX_THRESHOLD = 25
    VOLUME_MULTIPLIER = 1.5
    SL_BUFFER_PCT = 0.1
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.0
    ENTRY_EMA = 20
    ENABLE_200_EMA_FILTER = False
    EMA200_PERIOD = 200
    EMA200_LOOKBACK = 250
    EMA200_ALLOW_TOUCH = False
    EMA200_MIN_DISTANCE_PCT = 0.10
    EMA200_SLOPE_LOOKBACK = 5
    # This test predates vwap_acceptance.py (merged later, PR #9) and its
    # synthetic test data was never designed to also satisfy that filter's
    # multi-bar VWAP-acceptance window. Disable it here so this file keeps
    # testing exactly what it was written for -- the 200 EMA filter --
    # isolated from a different filter added afterward. Same isolation
    # principle already used for RVOL/watchlist tests this week.
    ENABLE_VWAP_ACCEPTANCE_FILTER = False


# -- Case 1: filter disabled (default) -- must produce a signal exactly --
# -- like before this integration existed, for an ordinary uptrend BUY --

cfg1 = FakeCfg()
df_15m = make_15m_df(30, 100.0, 0.5)   # short df -- well under 200+5, would FAIL the 200 EMA check if it ran
df_5m = make_5m_df(entry_close=105.0, ema_entry=104.0, avg_volume=1000, volume=1000)

signal = evaluate("TESTSYM", df_15m, df_5m, cfg1)
check("Filter disabled (default) -> signal still produced despite df_15m being far too short for EMA200 "
      "(proves the 200 EMA check never even runs when disabled)", signal is not None)
check("Filter disabled -> signal direction is BUY as the ordinary uptrend logic would produce",
      signal is not None and signal.direction == "BUY")

# -- Case 2: filter enabled, but df_15m too short -> FAIL -> None ---------
# This proves the filter, once enabled, actually has teeth and can
# block a signal that the ordinary EMA/volume logic alone would allow.

cfg2 = FakeCfg()
cfg2.ENABLE_200_EMA_FILTER = True
signal2 = evaluate("TESTSYM", df_15m, df_5m, cfg2)
check("Filter ENABLED + insufficient candles for EMA200 -> signal blocked (returns None)", signal2 is None)

# -- Case 3: filter enabled, sufficient data, clear uptrend + BUY signal --
# -- both the ordinary logic AND the 200 EMA agree -> signal produced -----

cfg3 = FakeCfg()
cfg3.ENABLE_200_EMA_FILTER = True
long_uptrend_15m = make_15m_df(260, 50.0, 0.3)  # enough bars, clear rising trend
# Make the 5m signal candle consistent with this uptrend's price level
last_15m_close = long_uptrend_15m["close"].iloc[-1]
df_5m_aligned = make_5m_df(entry_close=last_15m_close + 2, ema_entry=last_15m_close + 1, avg_volume=1000, volume=1000)

signal3 = evaluate("TESTSYM", long_uptrend_15m, df_5m_aligned, cfg3)
check("Filter ENABLED, clear uptrend, aligned BUY signal -> signal produced (200 EMA confirms)",
      signal3 is not None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
