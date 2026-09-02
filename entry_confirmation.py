"""
Price-action and ADX context for candidate ranking.

This module does not change capital, risk percentage, stop-loss,
profit target, position size, maximum positions, maximum trades or
daily-loss limits.

An opposing CHoCH is treated as a hard safety rejection. Other
price-action and ADX conditions adjust candidate ranking rather than
automatically preventing otherwise valid technical signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import pandas as pd

import config as cfg
from scheduler import candle_interval_minutes
from strategy import completed_15m_rows


@dataclass(frozen=True)
class EntryContext:
    accepted: bool
    score_adjustment: float
    reason: str
    detail: dict[str, Any]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not isfinite(result):
        return None

    return result


def _flag(value: Any) -> bool:
    if value is True:
        return True

    if value is False or value is None:
        return False

    if isinstance(value, dict):
        for key in (
            "confirmed",
            "detected",
            "matches_direction",
            "value",
        ):
            if key in value:
                return value[key] is True

    return False


def _adx_snapshot(
    df_15m: pd.DataFrame,
    as_of,
) -> tuple[
    float | None,
    float | None,
    float | None,
    str,
]:
    if (
        df_15m is None
        or df_15m.empty
        or "date" not in df_15m.columns
        or "adx" not in df_15m.columns
    ):
        return None, None, None, "UNAVAILABLE"

    completed = completed_15m_rows(df_15m, as_of)

    values = pd.to_numeric(
        completed["adx"],
        errors="coerce",
    ).dropna()

    if len(values) < 2:
        return None, None, None, "UNAVAILABLE"

    previous = _number(values.iloc[-2])
    current = _number(values.iloc[-1])

    if previous is None or current is None:
        return None, None, None, "UNAVAILABLE"

    delta = current - previous

    if delta > 0:
        state = "RISING"
    elif delta < 0:
        state = "FALLING"
    else:
        state = "FLAT"

    return current, previous, delta, state


def assess_entry_context(
    signal,
    df_15m: pd.DataFrame,
) -> EntryContext:
    price_action = (
        getattr(
            signal,
            "price_action_detail",
            None,
        )
        or {}
    )

    structure = _flag(
        price_action.get("market_structure")
    )

    breakout = _flag(
        price_action.get("breakout")
    )

    pullback = _flag(
        price_action.get("pullback")
    )

    bos = _flag(
        price_action.get("bos")
    )

    choch = _flag(
        price_action.get("choch")
    )

    signal_timestamp = getattr(signal, "timestamp", None)
    evaluation_time = (
        None
        if signal_timestamp is None
        else pd.Timestamp(signal_timestamp) + pd.Timedelta(
            minutes=candle_interval_minutes(cfg.ENTRY_TIMEFRAME)
        )
    )

    (
        current_adx,
        previous_adx,
        adx_delta,
        adx_state,
    ) = _adx_snapshot(df_15m, evaluation_time)

    confirmations = sum(
        (
            structure,
            breakout,
            pullback,
            bos,
        )
    )

    score = 0.0

    if structure:
        score += 8.0

    if breakout:
        score += 12.0

    if pullback:
        score += 15.0

    if bos:
        score += 10.0

    if confirmations == 0:
        score -= 10.0

    if adx_state == "RISING":
        score += 8.0
    elif adx_state == "FLAT":
        score += 1.0
    elif adx_state == "FALLING":
        score -= 6.0

    detail = {
        "market_structure": structure,
        "breakout": breakout,
        "pullback": pullback,
        "bos": bos,
        "choch": choch,
        "confirmation_count": confirmations,
        "adx_current": current_adx,
        "adx_previous": previous_adx,
        "adx_delta": adx_delta,
        "adx_state": adx_state,
    }

    if choch:
        return EntryContext(
            accepted=False,
            score_adjustment=round(
                score - 30.0,
                2,
            ),
            reason=(
                "opposing change of character detected"
            ),
            detail=detail,
        )

    if confirmations == 0:
        reason = (
            "no additional price-action confirmation; "
            "candidate retained with ranking penalty"
        )
    else:
        reason = (
            "price-action context supports candidate"
        )

    return EntryContext(
        accepted=True,
        score_adjustment=round(score, 2),
        reason=reason,
        detail=detail,
    )
