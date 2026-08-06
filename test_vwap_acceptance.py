import pandas as pd

from strategy import evaluate
from vwap_acceptance import FAIL, NOT_ENABLED, PASS, evaluate_vwap_acceptance


class Cfg:
    ENABLE_VWAP_ACCEPTANCE_FILTER = True
    VWAP_ACCEPTANCE_BARS = 2
    VWAP_ACCEPTANCE_REQUIRE_FULL_CANDLE = False
    ENABLE_200_EMA_FILTER = False
    USE_ADX_FILTER = False
    ADX_MODE = "off"
    ADX_THRESHOLD = 25
    VOLUME_MULTIPLIER = 1.2
    SL_BUFFER_PCT = 0.05
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.0
    ENTRY_EMA = 20


def make_15m(direction):
    date = pd.Timestamp("2026-08-07 10:00:00")
    if direction == "BUY":
        row = {"date": date, "close": 110.0, "ema_fast": 105.0, "ema_slow": 100.0, "vwap": 104.0, "adx": 30.0}
    else:
        row = {"date": date, "close": 90.0, "ema_fast": 95.0, "ema_slow": 100.0, "vwap": 96.0, "adx": 30.0}
    return pd.DataFrame([row])


def make_5m(direction, accepted=True):
    dates = [pd.Timestamp("2026-08-07 09:55:00"), pd.Timestamp("2026-08-07 10:00:00")]
    if direction == "BUY":
        closes = [106.0, 108.0] if accepted else [99.0, 108.0]
        vwaps = [104.0, 105.0]
        lows = [105.0, 107.0]
        highs = [107.0, 109.0]
        ema = [103.0, 104.0]
    else:
        closes = [94.0, 92.0] if accepted else [101.0, 92.0]
        vwaps = [96.0, 95.0]
        lows = [93.0, 91.0]
        highs = [95.0, 93.0]
        ema = [97.0, 96.0]
    return pd.DataFrame({
        "date": dates,
        "close": closes,
        "vwap": vwaps,
        "low": lows,
        "high": highs,
        "ema_entry": ema,
        "volume": [2000.0, 2200.0],
        "avg_volume": [1000.0, 1000.0],
    })


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


cfg = Cfg()

status, _ = evaluate_vwap_acceptance(make_5m("BUY", True), "BUY", cfg)
check("BUY two-candle VWAP acceptance passes", status == PASS)

status, _ = evaluate_vwap_acceptance(make_5m("BUY", False), "BUY", cfg)
check("BUY first candle below VWAP fails", status == FAIL)

status, _ = evaluate_vwap_acceptance(make_5m("SELL", True), "SELL", cfg)
check("SELL two-candle VWAP acceptance passes", status == PASS)

status, _ = evaluate_vwap_acceptance(make_5m("SELL", False), "SELL", cfg)
check("SELL first candle above VWAP fails", status == FAIL)

buy_signal = evaluate("TESTBUY", make_15m("BUY"), make_5m("BUY", True), cfg)
check("accepted BUY reaches strategy Signal", buy_signal is not None and buy_signal.direction == "BUY")

buy_rejected = evaluate("TESTBUY", make_15m("BUY"), make_5m("BUY", False), cfg)
check("unaccepted BUY is blocked in strategy", buy_rejected is None)

sell_signal = evaluate("TESTSELL", make_15m("SELL"), make_5m("SELL", True), cfg)
check("accepted SELL reaches strategy Signal", sell_signal is not None and sell_signal.direction == "SELL")

sell_rejected = evaluate("TESTSELL", make_15m("SELL"), make_5m("SELL", False), cfg)
check("unaccepted SELL is blocked in strategy", sell_rejected is None)

cfg.ENABLE_VWAP_ACCEPTANCE_FILTER = False
status, _ = evaluate_vwap_acceptance(make_5m("BUY", False), "BUY", cfg)
check("disabled filter returns NOT_ENABLED", status == NOT_ENABLED)
legacy_signal = evaluate("LEGACY", make_15m("BUY"), make_5m("BUY", False), cfg)
check("disabled filter preserves old signal path", legacy_signal is not None)

print("\n10 passed, 0 failed")
