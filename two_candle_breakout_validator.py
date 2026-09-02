"""Replay-only two-candle breakout validator. Never imported by live code."""

from __future__ import annotations

from typing import Any

import pandas as pd

from breakout_validator import BreakoutValidation
from breakout_validator import validate_breakout as validate_single


def validate_breakout(
    df: pd.DataFrame,
    direction: str,
    **kwargs: Any,
) -> BreakoutValidation:
    side = str(direction or "").upper()

    if df is None or len(df) < 22:
        return validate_single(df, side, **kwargs)

    previous = validate_single(df.iloc[:-1], side, **kwargs)
    current = validate_single(df, side, **kwargs)

    previous_metrics = previous.metrics
    current_metrics = current.metrics

    close = pd.to_numeric(df["close"], errors="coerce")

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    if side == "BUY":
        same_ema_direction = bool(
            ema9.iloc[-2] > ema21.iloc[-2]
            and ema9.iloc[-1] > ema21.iloc[-1]
        )
    elif side == "SELL":
        same_ema_direction = bool(
            ema9.iloc[-2] < ema21.iloc[-2]
            and ema9.iloc[-1] < ema21.iloc[-1]
        )
    else:
        same_ema_direction = False

    structure = any(
        bool(metrics.get("structure_confirmed"))
        for metrics in (previous_metrics, current_metrics)
    )
    volume = any(
        bool(metrics.get("volume_confirmed"))
        for metrics in (previous_metrics, current_metrics)
    )
    volatility = any(
        bool(metrics.get("volatility_confirmed"))
        for metrics in (previous_metrics, current_metrics)
    )
    clv = any(
        bool(metrics.get("clv_confirmed"))
        for metrics in (previous_metrics, current_metrics)
    )

    # Neither candle may exceed the configured maximum ATR.
    not_overextended = all(
        bool(metrics.get("not_overextended"))
        for metrics in (previous_metrics, current_metrics)
    )

    reasons = []
    if not same_ema_direction:
        reasons.append("TWO_CANDLE_EMA_DIRECTION_NOT_ALIGNED")
    if not structure:
        reasons.append("TWO_CANDLE_STRUCTURE_NOT_CONFIRMED")
    if not volume:
        reasons.append("TWO_CANDLE_VOLUME_NOT_CONFIRMED")
    if not volatility:
        reasons.append("TWO_CANDLE_ATR_NOT_CONFIRMED")
    if not not_overextended:
        reasons.append("TWO_CANDLE_BREAKOUT_OVEREXTENDED")
    if not clv:
        reasons.append("TWO_CANDLE_CLV_NOT_CONFIRMED")

    metrics = {
        "mode": "REPLAY_ONLY_TWO_CANDLE",
        "same_ema_direction": same_ema_direction,
        "structure_confirmed": structure,
        "volume_confirmed": volume,
        "volatility_confirmed": volatility,
        "not_overextended": not_overextended,
        "clv_confirmed": clv,
        "previous_candle": previous_metrics,
        "current_candle": current_metrics,
    }

    return BreakoutValidation(not reasons, side, reasons, metrics)
