"""
Strategy engine.

Trend filter (15-min):
  Uptrend   -> close > ema_fast > ema_slow AND close > vwap
  Downtrend -> close < ema_fast < ema_slow AND close < vwap
  If cfg.USE_ADX_FILTER is True, the trend is only considered valid
  when ADX (14) is above cfg.ADX_THRESHOLD — i.e. there's an actual
  trend happening, not just price drifting above/below the EMAs in a
  choppy market. This can be toggled off to compare with/without.

Entry trigger (5-min), only taken in the direction of the current
15-min trend:
  Long  -> bullish_engulfing AND close > ema_entry AND volume > avg_volume * VOLUME_MULTIPLIER
  Short -> bearish_engulfing AND close < ema_entry AND volume > avg_volume * VOLUME_MULTIPLIER

Stop-loss: signal candle's low (long) / high (short), with a small buffer.
Target: entry + (entry - stop) * RISK_REWARD_MIN  (i.e. minimum 1:2 reward:risk).
A signal is only valid if that minimum reward:risk is achievable.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from patterns import label_engulfing_patterns
from adx_confidence import resolve_adx_mode, adx_confidence


@dataclass
class Signal:
    symbol: str
    direction: str        # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    target: float
    timestamp: pd.Timestamp
    reason: str
    confidence: Optional[str] = None  # ADX dynamic-mode tier, or None if not applicable


def get_trend(row_15m: pd.Series, cfg=None) -> Optional[str]:
    if pd.isna(row_15m["ema_slow"]) or pd.isna(row_15m["vwap"]):
        return None

    if cfg is not None and resolve_adx_mode(cfg) == "binary":
        adx_value = row_15m.get("adx")
        if pd.isna(adx_value) or adx_value < getattr(cfg, "ADX_THRESHOLD", 25):
            return None  # market isn't trending strongly enough right now

    if row_15m["close"] > row_15m["ema_fast"] > row_15m["ema_slow"] and row_15m["close"] > row_15m["vwap"]:
        return "UP"
    if row_15m["close"] < row_15m["ema_fast"] < row_15m["ema_slow"] and row_15m["close"] < row_15m["vwap"]:
        return "DOWN"
    return None


def latest_completed_15m_trend(df_15m: pd.DataFrame, as_of: pd.Timestamp, cfg=None) -> Optional[str]:
    """Trend as of the most recently completed 15-min candle at or before `as_of`."""
    completed = df_15m[df_15m["date"] <= as_of]
    if completed.empty:
        return None
    return get_trend(completed.iloc[-1], cfg)


def get_trend_confidence(row_15m: pd.Series, cfg=None) -> Optional[str]:
    """
    Returns the ADX confidence tier for this row, independent of the
    existing binary USE_ADX_FILTER check in get_trend(). Does not
    affect get_trend()'s own decision -- purely additive/informational
    unless the caller (evaluate()) explicitly acts on "dynamic" mode.
    """
    if cfg is None:
        return None
    adx_value = row_15m.get("adx")
    return adx_confidence(adx_value, cfg)


def latest_completed_15m_confidence(df_15m: pd.DataFrame, as_of: pd.Timestamp, cfg=None) -> Optional[str]:
    """Companion to latest_completed_15m_trend() -- same row, confidence tier instead of direction."""
    completed = df_15m[df_15m["date"] <= as_of]
    if completed.empty:
        return None
    return get_trend_confidence(completed.iloc[-1], cfg)


def evaluate(symbol: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame, cfg) -> Optional[Signal]:
    """
    Evaluate the strategy on the latest completed 5-min candle.
    Returns a Signal if entry conditions are met, else None.
    """
    if len(df_5m) < 2 or len(df_15m) < 1:
        return None

    df_5m = label_engulfing_patterns(df_5m)
    curr = df_5m.iloc[-1]

    trend = latest_completed_15m_trend(df_15m, curr["date"], cfg)
    if trend is None:
        return None
    if pd.isna(curr["avg_volume"]) or pd.isna(curr["ema_entry"]):
        return None

    # Additive: only affects behavior when cfg.ADX_MODE == "dynamic".
    # In "off"/"binary" mode (today's default), confidence is computed
    # for visibility/labeling only and never blocks a trade here --
    # the existing get_trend() ADX check above already handles binary
    # mode's pass/fail entirely on its own, unchanged.
    confidence = latest_completed_15m_confidence(df_15m, curr["date"], cfg)
    if resolve_adx_mode(cfg) == "dynamic" and confidence == "REJECTED":
        return None

    volume_ok = curr["volume"] > curr["avg_volume"] * cfg.VOLUME_MULTIPLIER

    if trend == "UP" and curr["bullish_engulfing"] and curr["close"] > curr["ema_entry"] and volume_ok:
        entry = curr["close"]
        stop = curr["low"] * (1 - cfg.SL_BUFFER_PCT / 100)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + risk * cfg.RISK_REWARD_MIN
        reason = "15m uptrend + 5m bullish engulfing above EMA20 on above-avg volume"
        if getattr(cfg, "USE_ADX_FILTER", False):
            reason += " (ADX-confirmed trend)"
        if confidence:
            reason += f" [ADX confidence: {confidence}]"
        return Signal(symbol, "BUY", entry, stop, target, curr["date"], reason, confidence=confidence)

    if trend == "DOWN" and curr["bearish_engulfing"] and curr["close"] < curr["ema_entry"] and volume_ok:
        entry = curr["close"]
        stop = curr["high"] * (1 + cfg.SL_BUFFER_PCT / 100)
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - risk * cfg.RISK_REWARD_MIN
        reason = "15m downtrend + 5m bearish engulfing below EMA20 on above-avg volume"
        if getattr(cfg, "USE_ADX_FILTER", False):
            reason += " (ADX-confirmed trend)"
        if confidence:
            reason += f" [ADX confidence: {confidence}]"
        return Signal(symbol, "SELL", entry, stop, target, curr["date"], reason, confidence=confidence)

    return None
