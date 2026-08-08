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
    """Reshaped for the 3-step pullback trigger (was: simple rising-close
    breakout). Still deliberately has NO vwap column -- that remains the
    entire point of this test file (confirming VWAP acceptance broadcasts
    from the 15m timeframe instead of crashing).

    Only the LAST TWO bars matter to the pullback trigger (prev/curr);
    any earlier bars are just filler with the same general shape."""
    base = datetime(2026, 8, 7, 14, 45)
    prev_low = ema_entry - 1.0
    prev_close = ema_entry + 0.3
    prev_high = ema_entry + 0.5
    curr_close = entry_close
    rows = []
    for i in range(n_bars - 2):
        rows.append({
            "date": base + timedelta(minutes=5 * i),
            "open": prev_close - 0.2, "high": prev_close + 0.2, "low": prev_close - 0.4, "close": prev_close,
            "ema_entry": ema_entry, "avg_volume": avg_volume, "volume": avg_volume,
        })
    rows.append({
        "date": base + timedelta(minutes=5 * (n_bars - 2)),
        "open": prev_low + 0.2, "high": prev_high, "low": prev_low, "close": prev_close,
        "ema_entry": ema_entry, "avg_volume": avg_volume, "volume": avg_volume,
    })
    rows.append({
        "date": base + timedelta(minutes=5 * (n_bars - 1)),
        "open": prev_close, "high": curr_close + 0.3, "low": prev_close - 0.1, "close": curr_close,
        "ema_entry": ema_entry, "avg_volume": avg_volume, "volume": volume,
    })
    return pd.DataFrame(rows)


def make_index_15m(bullish=True):
    close = 25100 if bullish else 24900
    ema_fast = close - 20 if bullish else close + 20
    ema_slow = close - 50 if bullish else close + 50
    return pd.DataFrame([{
        "date": datetime(2026, 8, 7, 14, 45), "close": close,
        "vwap": float("nan"), "open": close, "high": close + 50, "low": close - 50,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "adx": 30.0,
    }])


INDEX_BULLISH = make_index_15m(bullish=True)
INDEX_BEARISH = make_index_15m(bullish=False)


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

signal = evaluate("TESTSYM", df_15m, df_5m, INDEX_BULLISH, cfg1)
check("FIX CONFIRMED: an otherwise-qualifying BUY setup now produces a real signal "
      "(previously always returned None due to the missing-column bug)", signal is not None)
if signal:
    check("Signal direction is BUY as expected", signal.direction == "BUY")

# -- Same for SELL ------------------------------------------------------------

cfg2 = FakeCfg()
df_15m_down = make_15m_df(10, 500.0, -0.5, vwap_offset=0.5)
last_15m_close_down = df_15m_down["close"].iloc[-1]
ema_entry_down = last_15m_close_down - 1
entry_close_down = last_15m_close_down - 2
base = datetime(2026, 8, 7, 14, 45)
# Reshaped for the pullback SELL sequence: prev high above ema_entry
# (Setup), prev close below ema_entry (Rejection), curr close below
# prev low (Confirmation) -- still no vwap column on df_5m.
df_5m_down = pd.DataFrame([
    {"date": base, "open": ema_entry_down - 0.1, "high": ema_entry_down + 1.0,
     "low": ema_entry_down - 0.8, "close": ema_entry_down - 0.3,
     "ema_entry": ema_entry_down, "avg_volume": 1000, "volume": 1000},
    {"date": base + timedelta(minutes=5), "open": ema_entry_down - 0.3, "high": ema_entry_down - 0.1,
     "low": entry_close_down - 0.2, "close": entry_close_down,
     "ema_entry": ema_entry_down, "avg_volume": 1000, "volume": 2000},
])

signal_sell = evaluate("TESTSYM", df_15m_down, df_5m_down, INDEX_BEARISH, cfg2)
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
    {"date": base + timedelta(minutes=5), "open": 100, "high": 103.5, "low": 95,
     # Confirmation requires close > prev.high (101); this close (103) also
     # stays BELOW the 15m vwap (104.5, from vwap_offset=-0.5 on a last
     # close of 105) -- satisfies the new pullback trigger while still
     # genuinely failing VWAP acceptance, preserving this test's purpose.
     "close": 103,
     "ema_entry": 99, "avg_volume": 1000, "volume": 2000},
])
df_15m_choppy = make_15m_df(10, 100.0, 0.5, vwap_offset=-0.5)
signal_choppy = evaluate("TESTSYM", df_15m_choppy, choppy_5m, INDEX_BULLISH, cfg3)
check("A genuine VWAP-acceptance failure (inconsistent closes) still correctly rejects, "
      "confirming the fix didn't just make the check a no-op", signal_choppy is None)

# -- Filter disabled -> never blocks, regardless of the column bug -----------

cfg5 = FakeCfg()
cfg5.ENABLE_VWAP_ACCEPTANCE_FILTER = False
check("ENABLE_VWAP_ACCEPTANCE_FILTER=False -> signal still produced even with no vwap column at all",
      evaluate("TESTSYM", df_15m, df_5m, INDEX_BULLISH, cfg5) is not None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
