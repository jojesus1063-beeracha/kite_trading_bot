"""Replay-only directional-close breakout validator."""

from __future__ import annotations

from typing import Any

import pandas as pd

from breakout_validator import BreakoutValidation
from breakout_validator import validate_breakout as validate_strict


def validate_breakout(
    df: pd.DataFrame,
    direction: str,
    **kwargs: Any,
) -> BreakoutValidation:
    strict = validate_strict(df, direction, **kwargs)
    metrics = dict(strict.metrics)

    structure = bool(metrics.get("structure_confirmed"))
    clv = bool(metrics.get("clv_confirmed"))
    volume = bool(metrics.get("volume_confirmed"))
    volatility = bool(metrics.get("volatility_confirmed"))
    not_overextended = bool(metrics.get("not_overextended"))

    reasons = []

    if not structure:
        reasons.append("STRUCTURE_BREAK_REQUIRED")
    if not clv:
        reasons.append("DIRECTIONAL_CLV_REQUIRED")
    if not (volume or volatility):
        reasons.append("VOLUME_OR_ATR_CONFIRMATION_REQUIRED")
    if not not_overextended:
        reasons.append("BREAKOUT_OVEREXTENDED_ATR")

    metrics.update({
        "mode": "REPLAY_ONLY_DIRECTIONAL_CLOSE",
        "volume_or_atr_confirmed": volume or volatility,
    })

    passed = (
        structure
        and clv
        and (volume or volatility)
        and not_overextended
    )

    return BreakoutValidation(
        passed=passed,
        direction=str(direction or "").upper(),
        reasons=reasons,
        metrics=metrics,
    )

