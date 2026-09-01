"""Replay-only scored same-candle breakout validation."""

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
    not_overextended = bool(metrics.get("not_overextended"))

    supporting = {
        "volume": bool(metrics.get("volume_confirmed")),
        "atr_expansion": bool(metrics.get("volatility_confirmed")),
        "directional_clv": bool(metrics.get("clv_confirmed")),
    }
    supporting_count = sum(supporting.values())

    reasons = []

    if not structure:
        reasons.append("STRUCTURE_BREAK_REQUIRED")

    if not not_overextended:
        reasons.append("BREAKOUT_OVEREXTENDED_ATR")

    if supporting_count < 2:
        reasons.append("REQUIRE_TWO_OF_VOLUME_ATR_CLV")

    metrics.update({
        "mode": "REPLAY_ONLY_STRUCTURE_PLUS_TWO_OF_THREE",
        "supporting_confirmations": supporting,
        "supporting_confirmation_count": supporting_count,
        "required_supporting_confirmations": 2,
    })

    passed = (
        structure
        and not_overextended
        and supporting_count >= 2
    )

    return BreakoutValidation(
        passed=passed,
        direction=str(direction or "").upper(),
        reasons=reasons,
        metrics=metrics,
    )
