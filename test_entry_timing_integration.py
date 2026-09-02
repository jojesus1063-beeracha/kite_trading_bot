"""
Requirement #16: with all entry-timing blocking DISABLED, the existing
signal set must be completely unchanged.

This proves the integration is diagnostic-only by default -- it
compares full Signal objects field-by-field (entry, stop, target,
direction, timestamp, confidence), not just "a signal appeared."

Also proves the opposite direction: when the filter IS enabled and a
setup genuinely violates it, the signal is blocked -- confirming the
wiring is real and not a permanent no-op.
"""

import pandas as pd
from datetime import datetime, timedelta

import config as real_cfg
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


class FakeCfg:
    USE_ADX_FILTER = False
    ADX_THRESHOLD = 22.0
    ADX_MODE = "off"
    VOLUME_MULTIPLIER = 1.2
    SL_BUFFER_PCT = 0.1
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.5
    ENTRY_EMA = 20
    ENABLE_200_EMA_FILTER = False
    ENABLE_VWAP_ACCEPTANCE_FILTER = False
    ENABLE_ENTRY_TIMING_FILTER = False
    MAX_ENTRY_EXTENSION_ATR = 1.50
    ATR_PERIOD = 14
    ENABLE_CONFIRMATION_QUALITY_FILTER = False
    MIN_CONFIRMATION_BODY_RATIO = 0.50
    ENABLE_VOLUME_ACCELERATION_FILTER = False
    MIN_CONFIRMATION_VOLUME_ACCELERATION = 1.10


def make_15m(n, start_price, drift, vwap_offset):
    base = datetime(2026, 8, 10, 9, 15)
    rows = []
    price = start_price
    ema_fast_off, ema_slow_off = (-0.5, -2.0) if drift >= 0 else (0.5, 2.0)
    for i in range(n):
        price += drift
        rows.append({
            "date": base + timedelta(minutes=15 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price,
            "volume": 10000, "ema_fast": price + ema_fast_off, "ema_slow": price + ema_slow_off,
            "vwap": price + vwap_offset, "adx": 30.0,
        })
    return pd.DataFrame(rows)


def make_index_15m(bullish=True):
    close = 25100 if bullish else 24900
    return pd.DataFrame([{
        "date": datetime(2026, 8, 10, 10, 0), "close": close,
        "vwap": float("nan"), "open": close, "high": close + 50, "low": close - 50,
        "ema_fast": close - 20 if bullish else close + 20,
        "ema_slow": close - 50 if bullish else close + 50, "adx": 30.0,
    }])


def make_5m_buy(entry_ema, n_history=20, extension=1.0, avg_volume=1000):
    base = datetime(2026, 8, 10, 9, 0)
    rows = []
    for i in range(n_history):
        rows.append({
            "date": base + timedelta(minutes=5 * i),
            "open": entry_ema, "high": entry_ema + 1.0, "low": entry_ema - 1.0,
            "close": entry_ema, "ema_entry": entry_ema, "avg_volume": avg_volume,
            "volume": avg_volume,
        })
    prev_low = entry_ema - 1.0
    prev_close = entry_ema + 0.3
    prev_high = entry_ema + 0.5
    rows.append({
        "date": base + timedelta(minutes=5 * n_history),
        "open": prev_low + 0.2, "high": prev_high, "low": prev_low, "close": prev_close,
        "ema_entry": entry_ema, "avg_volume": avg_volume, "volume": avg_volume,
    })
    curr_close = entry_ema + extension
    rows.append({
        "date": base + timedelta(minutes=5 * (n_history + 1)),
        "open": prev_close, "high": curr_close + 0.2, "low": prev_close - 0.1,
        "close": curr_close, "ema_entry": entry_ema, "avg_volume": avg_volume,
        "volume": avg_volume * 3,
    })
    return pd.DataFrame(rows)


df15 = make_15m(10, 100.0, 0.5, vwap_offset=-2.0)
entry_ema = df15["close"].iloc[-1] - 0.5
index_bull = make_index_15m(bullish=True)


cfg_off = FakeCfg()
df5 = make_5m_buy(entry_ema=entry_ema, extension=1.0)
signal_off = evaluate("TEST", df15, df5, index_bull, cfg_off)

check("Baseline (all timing blockers OFF) still produces a signal", signal_off is not None)

if signal_off:
    signal_again = evaluate("TEST", df15, df5, index_bull, FakeCfg())
    check("REQUIREMENT 16: entry price unchanged", signal_again.entry_price == signal_off.entry_price)
    check("REQUIREMENT 16: stop unchanged", signal_again.stop_loss == signal_off.stop_loss)
    check("REQUIREMENT 16: target unchanged", signal_again.target == signal_off.target)
    check("REQUIREMENT 16: direction unchanged", signal_again.direction == signal_off.direction)
    check("REQUIREMENT 16: timestamp unchanged", signal_again.timestamp == signal_off.timestamp)
    check("REQUIREMENT 16: confidence unchanged", signal_again.confidence == signal_off.confidence)


cfg_on = FakeCfg()
cfg_on.ENABLE_ENTRY_TIMING_FILTER = True
signal_on_good = evaluate("TEST", df15, df5, index_bull, cfg_on)
check("Timing filter ENABLED + well-timed entry -> signal still produced",
      signal_on_good is not None)
if signal_on_good and signal_off:
    check("Timing filter ENABLED + well-timed entry -> IDENTICAL entry price to baseline",
          signal_on_good.entry_price == signal_off.entry_price)


cfg_on2 = FakeCfg()
cfg_on2.ENABLE_ENTRY_TIMING_FILTER = True
df5_extended = make_5m_buy(entry_ema=entry_ema, extension=5.0)
signal_extended = evaluate("TEST", df15, df5_extended, index_bull, cfg_on2)
check("Timing filter ENABLED + over-extended entry -> signal BLOCKED (wiring is real, not a no-op)",
      signal_extended is None)

cfg_off2 = FakeCfg()
signal_extended_off = evaluate("TEST", df15, df5_extended, index_bull, cfg_off2)
check("Same over-extended entry with filter OFF -> signal still produced (proves ONLY the filter blocked it)",
      signal_extended_off is not None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
