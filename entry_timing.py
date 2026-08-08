"""
Version D -- Entry Timing Layer.

Evaluates whether an already-fully-qualified trade candidate (stock
geometry passed, macro authorized, VWAP/EMA200/RVOL all passed)
represents a well-timed executable entry, or whether the confirmation
move has already become extended.

DESIGN CONSTRAINTS, verified against the real codebase before writing:

1. df_5m has ONLY "ema_entry" and "avg_volume" attached by
   add_indicators() (indicators.py) -- NO atr column and NO vwap
   column. This module therefore computes ATR locally from the OHLC
   data already present in df_5m rather than reading a column that
   does not exist. (Referencing a nonexistent column is exactly the
   bug that silently blocked every signal for days -- see the
   vwap_acceptance missing-column incident.)

2. This layer runs LAST, after every existing gate. It never
   substitutes for, weakens, or bypasses any of them.

3. Every filter is individually switchable. ENABLE_VOLUME_ACCELERATION_FILTER
   defaults to False per spec -- it must be MEASURED in replay before
   being made mandatory.

4. No look-ahead: only candles at or before the decision timestamp are
   used. ATR is computed from completed candles only.

Classifications: OPTIMAL / ACCEPTABLE / LATE / INVALID.
Only INVALID blocks a trade (and only when the relevant filter is
enabled). LATE is recorded for analysis but does not block, per the
spec's explicit instruction not to reject solely for non-OPTIMAL
timing unless policy requires it.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("entry_timing")

OPTIMAL = "OPTIMAL"
ACCEPTABLE = "ACCEPTABLE"
LATE = "LATE"
INVALID = "INVALID"
NOT_ENABLED = "NOT_ENABLED"


def _local_atr(df_5m: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    ATR computed locally from df_5m's OHLC -- df_5m has no "atr" column
    (verified against add_indicators()). Uses only the rows present in
    the passed slice, which the caller has already bounded to the
    decision timestamp, so there is no look-ahead.
    """
    if df_5m is None or len(df_5m) < period + 1:
        return None
    needed = {"high", "low", "close"}
    if not needed.issubset(df_5m.columns):
        return None
    high = df_5m["high"]
    low = df_5m["low"]
    prev_close = df_5m["close"].shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    value = true_range.rolling(window=period).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def _confirmation_quality(curr) -> dict:
    """Body/range geometry of the confirmation candle itself."""
    high = float(curr["high"])
    low = float(curr["low"])
    open_ = float(curr["open"])
    close = float(curr["close"])
    candle_range = high - low
    body_size = abs(close - open_)
    body_to_range = (body_size / candle_range) if candle_range > 0 else 0.0
    return {
        "body_size": round(body_size, 4),
        "candle_range": round(candle_range, 4),
        "body_to_range_ratio": round(body_to_range, 4),
        "is_bullish_candle": close > open_,
        "is_bearish_candle": close < open_,
    }


def evaluate_entry_timing(symbol: str, direction: str, df_5m: pd.DataFrame, curr, prev, cfg) -> tuple:
    """
    Returns (classification, detail).

    classification is NOT_ENABLED / OPTIMAL / ACCEPTABLE / LATE / INVALID.
    Only INVALID indicates the caller should reject the signal.

    Never raises -- any unexpected condition degrades to a
    non-blocking classification with the reason recorded, matching the
    fail-safe convention used by the other filter modules in this
    codebase.
    """
    if not getattr(cfg, "ENABLE_ENTRY_TIMING_FILTER", False):
        return NOT_ENABLED, {"reason": "entry timing filter disabled"}

    try:
        detail: dict = {"direction": direction}
        entry_price = float(curr["close"])
        ema_entry = float(curr["ema_entry"]) if not pd.isna(curr["ema_entry"]) else None

        # -- Anti-chase: ATR-normalized distance from the entry EMA ----
        # ATR-normalized rather than a fixed percentage so the rule
        # behaves consistently across a Rs.100 stock and a Rs.2000 one.
        atr_value = _local_atr(df_5m, getattr(cfg, "ATR_PERIOD", 14))
        detail["atr"] = None if atr_value is None else round(atr_value, 4)
        detail["ema_entry"] = ema_entry
        detail["entry_price"] = entry_price

        extension_atr = None
        if atr_value is not None and atr_value > 0 and ema_entry is not None:
            raw_distance = (entry_price - ema_entry) if direction == "BUY" else (ema_entry - entry_price)
            extension_atr = raw_distance / atr_value
            detail["extension_atr"] = round(extension_atr, 4)

        max_extension_atr = getattr(cfg, "MAX_ENTRY_EXTENSION_ATR", 1.50)
        detail["max_entry_extension_atr"] = max_extension_atr

        extension_invalid = extension_atr is not None and extension_atr > max_extension_atr
        detail["extension_ok"] = not extension_invalid

        # -- Confirmation candle quality --------------------------------
        quality = _confirmation_quality(curr)
        detail.update(quality)
        min_body_ratio = getattr(cfg, "MIN_CONFIRMATION_BODY_RATIO", 0.50)
        detail["min_confirmation_body_ratio"] = min_body_ratio

        direction_ok = quality["is_bullish_candle"] if direction == "BUY" else quality["is_bearish_candle"]
        body_ok = quality["body_to_range_ratio"] >= min_body_ratio
        quality_ok = direction_ok and body_ok
        detail["confirmation_direction_ok"] = direction_ok
        detail["confirmation_body_ok"] = body_ok

        quality_filter_on = getattr(cfg, "ENABLE_CONFIRMATION_QUALITY_FILTER", False)
        detail["confirmation_quality_filter_enabled"] = quality_filter_on

        # -- Volume acceleration (separate from, never replacing, RVOL) --
        volume_acceleration = None
        if prev is not None and float(prev["volume"]) > 0:
            volume_acceleration = float(curr["volume"]) / float(prev["volume"])
            detail["volume_acceleration"] = round(volume_acceleration, 4)
        min_vol_accel = getattr(cfg, "MIN_CONFIRMATION_VOLUME_ACCELERATION", 1.10)
        detail["min_confirmation_volume_acceleration"] = min_vol_accel
        vol_accel_on = getattr(cfg, "ENABLE_VOLUME_ACCELERATION_FILTER", False)
        detail["volume_acceleration_filter_enabled"] = vol_accel_on
        vol_accel_ok = volume_acceleration is not None and volume_acceleration >= min_vol_accel
        detail["volume_acceleration_ok"] = vol_accel_ok

        # -- Blocking decision -------------------------------------------
        # Only enabled filters can produce INVALID. Everything else is
        # measured and recorded but never blocks, so replay can quantify
        # each filter's effect before it is switched on.
        blocking_reasons = []
        if extension_invalid:
            blocking_reasons.append("ENTRY_EXTENSION_TOO_HIGH")
        if quality_filter_on and not quality_ok:
            blocking_reasons.append("CONFIRMATION_BODY_TOO_WEAK")
        if vol_accel_on and not vol_accel_ok:
            blocking_reasons.append("VOLUME_ACCELERATION_TOO_LOW")

        if blocking_reasons:
            detail["classification"] = INVALID
            detail["blocking_reasons"] = blocking_reasons
            return INVALID, detail

        # -- Non-blocking classification for analysis --------------------
        if extension_atr is None:
            classification = ACCEPTABLE  # insufficient data to grade timing; never blocks
            detail["reason"] = "ATR or entry EMA unavailable -- timing not graded"
        elif extension_atr <= max_extension_atr * 0.5 and quality_ok:
            classification = OPTIMAL
        elif extension_atr <= max_extension_atr * 0.8:
            classification = ACCEPTABLE
        else:
            classification = LATE

        detail["classification"] = classification
        return classification, detail

    except Exception as exc:
        logger.exception("entry_timing: evaluate_entry_timing failed unexpectedly -- "
                          "degrading to non-blocking ACCEPTABLE rather than silently killing a signal")
        return ACCEPTABLE, {"direction": direction, "reason": f"unexpected error: {exc}",
                            "classification": ACCEPTABLE}


def format_entry_timing_log(symbol: str, classification: str, detail: dict) -> str:
    if classification == NOT_ENABLED:
        return f"{symbol}: ENTRY_TIMING | DISABLED"
    parts = [f"{symbol}: ENTRY_TIMING", f"classification={classification}",
             f"direction={detail.get('direction')}"]
    if detail.get("extension_atr") is not None:
        parts.append(f"extension_atr={detail['extension_atr']}"
                      f"/{detail.get('max_entry_extension_atr')}")
    if detail.get("body_to_range_ratio") is not None:
        parts.append(f"body_ratio={detail['body_to_range_ratio']}")
    if detail.get("volume_acceleration") is not None:
        parts.append(f"vol_accel={detail['volume_acceleration']}")
    if detail.get("blocking_reasons"):
        parts.append(f"BLOCKED_BY={','.join(detail['blocking_reasons'])}")
    return " | ".join(parts)
