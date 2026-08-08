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
        # prev low lowered to <= ema_entry (103) to satisfy the new
        # pullback Setup gate -- was 105.0, which satisfied neither
        # ema_entry nor vwap. Everything else (close/high/ema/volume)
        # already coincidentally satisfied Rejection/Confirmation/Volume
        # unchanged, so only this one value needed to move.
        lows = [102.0, 107.0]
        highs = [107.0, 109.0]
        ema = [103.0, 104.0]
    else:
        closes = [94.0, 92.0] if accepted else [101.0, 92.0]
        vwaps = [96.0, 95.0]
        # prev high raised to >= ema_entry (97) to satisfy the new
        # pullback Setup gate -- was 95.0, which satisfied neither.
        highs = [98.0, 93.0]
        lows = [93.0, 91.0]
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


def make_index_15m(bullish=True):
    close = 25100 if bullish else 24900
    ema_fast = close - 20 if bullish else close + 20
    ema_slow = close - 50 if bullish else close + 50
    return pd.DataFrame([{
        "date": pd.Timestamp("2026-08-07 10:00:00"), "close": close,
        "vwap": float("nan"), "open": close, "high": close + 50, "low": close - 50,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "adx": 30.0,
    }])


INDEX_BULLISH = make_index_15m(bullish=True)
INDEX_BEARISH = make_index_15m(bullish=False)


def make_5m_pullback_valid_but_vwap_rejected():
    """Satisfies the full pullback sequence (Setup/Rejection/Confirmation/
    Volume) but deliberately fails VWAP acceptance's own check (first
    candle's close sits BELOW vwap) -- used specifically to prove that
    disabling ENABLE_VWAP_ACCEPTANCE_FILTER bypasses that one check while
    everything else about the signal remains valid. The original
    make_5m("BUY", False) shape can no longer serve this purpose: its
    prev.close (99) now also independently fails the new pullback
    Rejection gate, which would make this test pass for the wrong
    reason regardless of the VWAP filter's enabled state."""
    dates = [pd.Timestamp("2026-08-07 09:55:00"), pd.Timestamp("2026-08-07 10:00:00")]
    return pd.DataFrame({
        "date": dates,
        "close": [103.5, 108.0],  # prev.close (103.5) > ema_entry (103, rejection OK) but < vwap (104, VWAP-acceptance fails)
        "vwap": [104.0, 105.0],
        "low": [102.0, 107.0],    # prev.low <= ema_entry -> Setup OK
        "high": [107.0, 109.0],   # curr.close (108) > prev.high (107) -> Confirmation OK
        "ema_entry": [103.0, 104.0],
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

buy_signal = evaluate("TESTBUY", make_15m("BUY"), make_5m("BUY", True), INDEX_BULLISH, cfg)
check("accepted BUY reaches strategy Signal", buy_signal is not None and buy_signal.direction == "BUY")

buy_rejected = evaluate("TESTBUY", make_15m("BUY"), make_5m("BUY", False), INDEX_BULLISH, cfg)
check("unaccepted BUY is blocked in strategy", buy_rejected is None)

sell_signal = evaluate("TESTSELL", make_15m("SELL"), make_5m("SELL", True), INDEX_BEARISH, cfg)
check("accepted SELL reaches strategy Signal", sell_signal is not None and sell_signal.direction == "SELL")

sell_rejected = evaluate("TESTSELL", make_15m("SELL"), make_5m("SELL", False), INDEX_BEARISH, cfg)
check("unaccepted SELL is blocked in strategy", sell_rejected is None)

cfg.ENABLE_VWAP_ACCEPTANCE_FILTER = False
status, _ = evaluate_vwap_acceptance(make_5m("BUY", False), "BUY", cfg)
check("disabled filter returns NOT_ENABLED", status == NOT_ENABLED)
legacy_signal = evaluate("LEGACY", make_15m("BUY"), make_5m_pullback_valid_but_vwap_rejected(), INDEX_BULLISH, cfg)
check("disabled filter preserves old signal path", legacy_signal is not None)

print("\n10 passed, 0 failed")
