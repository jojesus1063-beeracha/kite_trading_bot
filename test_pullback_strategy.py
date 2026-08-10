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


class FakeCfg:
    NO_ENTRY_BEFORE = "09:30"
    USE_ADX_FILTER = False
    ADX_THRESHOLD = 25
    ADX_MODE = "off"
    VOLUME_MULTIPLIER = 1.5
    SL_BUFFER_PCT = 0.1
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.0
    ENTRY_EMA = 20
    ENABLE_200_EMA_FILTER = False
    ENABLE_VWAP_ACCEPTANCE_FILTER = False
    PAPER_TRADING = True
    TREND_GATE_MODE = "enforce"
    PULLBACK_GATE_MODE = "enforce"
    EXPERIMENTAL_PAPER_ONLY = True


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
    # Matches get_trend(..., require_vwap=False)'s actual requirements:
    # close/ema_fast/ema_slow alignment, NOT vwap (indices never have
    # real volume, so vwap is deliberately irrelevant here now).
    close = 25100 if bullish else 24900
    ema_fast = close - 20 if bullish else close + 20
    ema_slow = close - 50 if bullish else close + 50
    return pd.DataFrame([{
        "date": datetime(2026, 8, 10, 9, 30), "close": close,
        "vwap": float("nan"),  # deliberately NaN -- proves the fix no longer depends on this
        "open": close, "high": close + 50, "low": close - 50,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "adx": 30.0,
    }])


def make_5m_pullback_buy(entry_ema=100.0, avg_volume=1000, satisfies_setup=True,
                          satisfies_rejection=True, satisfies_confirmation=True,
                          satisfies_volume=True, ts=None):
    base = ts or datetime(2026, 8, 10, 10, 0)
    prev_low = entry_ema - 0.5 if satisfies_setup else entry_ema + 2.0  # setup: prev low <= ema_entry
    prev_close = entry_ema + 0.3 if satisfies_rejection else entry_ema - 0.3  # rejection: prev close > ema_entry
    prev_high = prev_close + 0.5
    curr_close = prev_high + 0.5 if satisfies_confirmation else prev_high - 1.0  # confirmation: curr close > prev high
    prev_volume = 1200
    curr_volume = (avg_volume * 2.0) if satisfies_volume else (avg_volume * 0.5)
    return pd.DataFrame([
        {"date": base - timedelta(minutes=5), "open": prev_low + 0.2, "high": prev_high, "low": prev_low,
         "close": prev_close, "ema_entry": entry_ema, "avg_volume": avg_volume, "volume": prev_volume},
        {"date": base, "open": prev_close, "high": curr_close + 0.2, "low": prev_close - 0.1,
         "close": curr_close, "ema_entry": entry_ema, "avg_volume": avg_volume, "volume": curr_volume},
    ])


# -- Full valid BUY pullback sequence -> signal produced --------------------

cfg1 = FakeCfg()
df15 = make_15m(10, 100.0, 0.5, vwap_offset=-2.0)  # uptrend, vwap well below price
df5 = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5)
index_bull = make_index_15m(bullish=True)
signal = evaluate("TEST", df15, df5, index_bull, cfg1)
check("Full valid BUY pullback sequence -> signal produced", signal is not None)
if signal:
    check("Signal direction is BUY", signal.direction == "BUY")
    check("Stop computed from prev low, not curr low", signal.stop_loss < signal.entry_price)

# -- Missing Setup -> rejected ------------------------------------------------

cfg2 = FakeCfg()
df5_no_setup = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, satisfies_setup=False)
signal2 = evaluate("TEST", df15, df5_no_setup, index_bull, cfg2)
check("Missing Setup condition -> correctly rejected", signal2 is None)

# -- Missing Rejection -> rejected --------------------------------------------

cfg3 = FakeCfg()
df5_no_rejection = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, satisfies_rejection=False)
signal3 = evaluate("TEST", df15, df5_no_rejection, index_bull, cfg3)
check("Missing Rejection condition -> correctly rejected", signal3 is None)

# -- Missing Confirmation -> rejected ------------------------------------------

cfg4 = FakeCfg()
df5_no_confirm = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, satisfies_confirmation=False)
signal4 = evaluate("TEST", df15, df5_no_confirm, index_bull, cfg4)
check("Missing Confirmation condition -> correctly rejected", signal4 is None)

# -- Missing volume condition -> rejected --------------------------------------

cfg5 = FakeCfg()
df5_no_vol = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, satisfies_volume=False)
signal5 = evaluate("TEST", df15, df5_no_vol, index_bull, cfg5)
check("Insufficient volume -> correctly rejected", signal5 is None)

# -- Index bearish blocks an otherwise-valid BUY -------------------------------

cfg6 = FakeCfg()
index_bear = make_index_15m(bullish=False)
signal6 = evaluate("TEST", df15, df5, index_bear, cfg6)
check("Otherwise-valid BUY sequence blocked by bearish index", signal6 is None)

# -- Time filter follows the configured 09:30 entry boundary ------------------

cfg7 = FakeCfg()
early_ts = datetime(2026, 8, 10, 9, 25)
df5_early = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, ts=early_ts)
index_early = make_index_15m(bullish=True)
index_early.loc[:, "date"] = datetime(2026, 8, 10, 9, 15)
signal7 = evaluate("TEST", df15, df5_early, index_early, cfg7)
check("Before configured 09:30 -> blocked by time filter", signal7 is None)

# -- At configured 09:30 -> time filter no longer blocks ----------------------

cfg8 = FakeCfg()
ok_ts = datetime(2026, 8, 10, 9, 30)
df5_ok_time = make_5m_pullback_buy(entry_ema=df15["close"].iloc[-1] - 0.5, ts=ok_ts)
signal8 = evaluate("TEST", df15, df5_ok_time, index_early, cfg8)
check("At configured 09:30 -> time filter does not block", signal8 is not None)

# -- No index data -> fails safe, not a crash ----------------------------------

cfg9 = FakeCfg()
try:
    signal9 = evaluate("TEST", df15, df5, pd.DataFrame(), cfg9)
    check("Empty index dataframe -> fails safe (None), no crash", signal9 is None)
except Exception as e:
    check(f"Should never raise on empty index data, but got: {e}", False)

try:
    signal10 = evaluate("TEST", df15, df5, None, cfg9)
    check("None index dataframe -> fails safe (None), no crash", signal10 is None)
except Exception as e:
    check(f"Should never raise on None index data, but got: {e}", False)

# -- Paper observation mode bypasses strict setup/rejection/volume ----------
cfg_observe = FakeCfg()
cfg_observe.PULLBACK_GATE_MODE = "observe"
df5_observe = make_5m_pullback_buy(
    entry_ema=df15["close"].iloc[-1] - 0.5,
    satisfies_setup=True,
    satisfies_rejection=True,
    satisfies_confirmation=True,
    satisfies_volume=False,
)
signal_observe = evaluate("TEST", df15, df5_observe, index_bull, cfg_observe)
check("Observe-only pullback allows the baseline breakout", signal_observe is not None)

# Confirmation remains the deliberately simple replacement trigger.
df5_no_baseline = make_5m_pullback_buy(
    entry_ema=df15["close"].iloc[-1] - 0.5,
    satisfies_confirmation=False,
)
signal_no_baseline = evaluate("TEST", df15, df5_no_baseline, index_bull, cfg_observe)
check("Observe-only pullback still requires baseline breakout", signal_no_baseline is None)

# Trend observation uses EMA ordering only as baseline direction.
cfg_trend_observe = FakeCfg()
cfg_trend_observe.TREND_GATE_MODE = "observe"
df15_strict_trend_fail = df15.copy()
df15_strict_trend_fail["ema_fast"] = df15_strict_trend_fail["close"] + 0.5
df15_strict_trend_fail["ema_slow"] = df15_strict_trend_fail["close"] - 2.0
signal_trend_observe = evaluate(
    "TEST", df15_strict_trend_fail, df5, index_bull, cfg_trend_observe
)
check("Observe-only trend uses EMA ordering for baseline direction", signal_trend_observe is not None)

# Live mode always fails closed.
cfg_live_observe = FakeCfg()
cfg_live_observe.PAPER_TRADING = False
cfg_live_observe.TREND_GATE_MODE = "observe"
signal_live_observe = evaluate("TEST", df15, df5, index_bull, cfg_live_observe)
check("Observe-only gates are blocked outside paper mode", signal_live_observe is None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
