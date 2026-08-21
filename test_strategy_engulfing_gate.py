from types import SimpleNamespace

import pandas as pd

from strategy import evaluate


def _cfg():
    return SimpleNamespace(
        USE_ADX_FILTER=False,
        ADX_MODE="off",
        ADX_THRESHOLD=25,
        ADX_DYNAMIC_MIN=20,
        ADX_DYNAMIC_STRONG=35,
        VOLUME_MULTIPLIER=1.2,
        SL_BUFFER_PCT=0.05,
        SL_BUFFER_PCT_SELL=None,
        RISK_REWARD_MIN=2.0,
    )


def _trend_df(direction="UP"):
    if direction == "UP":
        row = {"date": pd.Timestamp("2026-08-21 10:00"), "close": 110.0, "ema_fast": 105.0, "ema_slow": 100.0, "vwap": 104.0, "adx": 30.0}
    else:
        row = {"date": pd.Timestamp("2026-08-21 10:00"), "close": 90.0, "ema_fast": 95.0, "ema_slow": 100.0, "vwap": 96.0, "adx": 30.0}
    return pd.DataFrame([row])


def test_uptrend_without_bullish_engulfing_is_rejected():
    df_5m = pd.DataFrame([
        {"date": pd.Timestamp("2026-08-21 09:55"), "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000, "avg_volume": 500, "ema_entry": 99.0},
        # Bullish, but previous candle is also bullish: NOT an engulfing pattern.
        {"date": pd.Timestamp("2026-08-21 10:00"), "open": 101.0, "high": 111.0, "low": 100.0, "close": 110.0, "volume": 1000, "avg_volume": 500, "ema_entry": 104.0},
    ])
    assert evaluate("ABC", _trend_df("UP"), df_5m, _cfg()) is None


def test_valid_bullish_engulfing_can_generate_signal():
    df_5m = pd.DataFrame([
        {"date": pd.Timestamp("2026-08-21 09:55"), "open": 105.0, "high": 106.0, "low": 100.0, "close": 102.0, "volume": 500, "avg_volume": 500, "ema_entry": 101.0},
        {"date": pd.Timestamp("2026-08-21 10:00"), "open": 101.0, "high": 111.0, "low": 100.0, "close": 110.0, "volume": 1000, "avg_volume": 500, "ema_entry": 104.0},
    ])
    signal = evaluate("ABC", _trend_df("UP"), df_5m, _cfg())
    assert signal is not None
    assert signal.direction == "BUY"
    assert "bullish engulfing" in signal.reason
