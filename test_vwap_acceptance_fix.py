"""
Reproduces the exact production bug (VWAP_ACCEPTANCE always failing with
"missing columns: vwap" because df_5m never has a vwap column) and
proves the fix resolves it using strategy.evaluate() itself -- the real
code path, not a mock.
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


def make_15m_df(n, start_price, drift, vwap_offset=-0.3, adx=30.0):
    base = datetime(2026, 8, 7, 9, 15)
    rows = []
    price = start_price
    # For an UP trend, get_trend() requires close > ema_fast > ema_slow
    # (both EMAs below price, fast closer to price than slow). For DOWN,
    # it requires close < ema_fast < ema_slow (both EMAs ABOVE price,
    # fast closer to price than slow).
    if drift >= 0:
        ema_fast_offset, ema_slow_offset = -0.5, -2.0
    else:
        ema_fast_offset, ema_slow_offset = 0.5, 2.0
    for i in range(n):
        price += drift
        rows.append({
            "date": base + timedelta(minutes=15 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price,
            "volume": 10000, "ema_fast": price + ema_fast_offset, "ema_slow": price + ema_slow_offset,
            "vwap": price + vwap_offset, "adx": adx,
        })
    return pd.DataFrame(rows)


def make_5m_df(entry_close, ema_entry, avg_volume, volume, n_bars=3):
    """Multiple bars, all closing above ema_entry with rising volume on
    the last bar -- matches an ordinary qualifying BUY setup, WITHOUT a
    vwap column, exactly like what add_indicators() actually produces."""
    base = datetime(2026, 8, 7, 14, 45)
    rows = []
    for i in range(n_bars):
        c = entry_close - (n_bars - 1 - i) * 0.1
        rows.append({
            "date": base + timedelta(minutes=5 * i),
            "open": c - 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
            "ema_entry": ema_entry, "avg_volume": avg_volume,
            "volume": volume if i == n_bars - 1 else avg_volume,
        })
    return pd.DataFrame(rows)


class FakeCfg:
    USE_ADX_FILTER = False
    ADX_THRESHOLD = 25
    ADX_MODE = "off"
    VOLUME_MULTIPLIER = 1.5
    SL_BUFFER_PCT = 0.1
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.0
    ENTRY_EMA = 20
    ENABLE_200_EMA_FILTER = False
    ENABLE_VWAP_ACCEPTANCE_FILTER = True
    VWAP_ACCEPTANCE_BARS = 2
    VWAP_ACCEPTANCE_REQUIRE_FULL_CANDLE = False


# -- Reproduce the exact production bug scenario ---------------------------
# 15m trend: clear uptrend, close well above VWAP -- an otherwise
# perfectly qualifying BUY setup by every other check.

cfg1 = FakeCfg()
df_15m = make_15m_df(10, 100.0, 0.5, vwap_offset=-0.5)
# entry_close set consistent with the 15m uptrend's price level
last_15m_close = df_15m["close"].iloc[-1]
df_5m = make_5m_df(entry_close=last_15m_close + 2, ema_entry=last_15m_close + 1,
                    avg_volume=1000, volume=2000)

check("Sanity check: df_5m genuinely has no 'vwap' column, matching real add_indicators() output",
      "vwap" not in df_5m.columns)

signal = evaluate("TESTSYM", df_15m, df_5m, cfg1)
check("FIX CONFIRMED: an otherwise-qualifying BUY setup now produces a real signal "
      "(previously always returned None due to the missing-column bug)", signal is not None)
if signal:
    check("Signal direction is BUY as expected", signal.direction == "BUY")

# -- Same for SELL ------------------------------------------------------------

cfg2 = FakeCfg()
df_15m_down = make_15m_df(10, 500.0, -0.5, vwap_offset=0.5)
last_15m_close_down = df_15m_down["close"].iloc[-1]
df_5m_down = make_5m_df(entry_close=last_15m_close_down - 2, ema_entry=last_15m_close_down - 1,
                         avg_volume=1000, volume=2000)
# Rebuild closes as a falling sequence for a valid SELL VWAP-acceptance window
base = datetime(2026, 8, 7, 14, 45)
df_5m_down = pd.DataFrame([
    {"date": base, "open": last_15m_close_down - 1.5, "high": last_15m_close_down - 1,
     "low": last_15m_close_down - 2.5, "close": last_15m_close_down - 2.3,
     "ema_entry": last_15m_close_down - 1, "avg_volume": 1000, "volume": 1000},
    {"date": base + timedelta(minutes=5), "open": last_15m_close_down - 2.3, "high": last_15m_close_down - 2,
     "low": last_15m_close_down - 3, "close": last_15m_close_down - 2.5,
     "ema_entry": last_15m_close_down - 1, "avg_volume": 1000, "volume": 2000},
])

signal_sell = evaluate("TESTSYM", df_15m_down, df_5m_down, cfg2)
check("FIX CONFIRMED: an otherwise-qualifying SELL setup now produces a real signal",
      signal_sell is not None)
if signal_sell:
    check("Signal direction is SELL as expected", signal_sell.direction == "SELL")

# -- The fix must not silently PASS bad setups -- a genuine VWAP-acceptance --
# -- failure (price NOT consistently on the correct side) must still reject --

cfg3 = FakeCfg()
choppy_5m = pd.DataFrame([
    {"date": base, "open": 99, "high": 101, "low": 98, "close": 100,
     "ema_entry": 99, "avg_volume": 1000, "volume": 1000},
    {"date": base + timedelta(minutes=5), "open": 100, "high": 102, "low": 95,
     "close": 96,  # this close is BELOW where a rising-uptrend VWAP would sit -- genuine rejection case
     "ema_entry": 99, "avg_volume": 1000, "volume": 2000},
])
df_15m_choppy = make_15m_df(10, 100.0, 0.5, vwap_offset=-0.5)
signal_choppy = evaluate("TESTSYM", df_15m_choppy, choppy_5m, cfg3)
check("A genuine VWAP-acceptance failure (inconsistent closes) still correctly rejects, "
      "confirming the fix didn't just make the check a no-op", signal_choppy is None)

# -- Filter disabled -> never blocks, regardless of the column bug -----------

cfg5 = FakeCfg()
cfg5.ENABLE_VWAP_ACCEPTANCE_FILTER = False
check("ENABLE_VWAP_ACCEPTANCE_FILTER=False -> signal still produced even with no vwap column at all",
      evaluate("TESTSYM", df_15m, df_5m, cfg5) is not None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
