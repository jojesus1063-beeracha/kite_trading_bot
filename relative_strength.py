"""
Benchmark relative-strength ranking.

This module does not generate or reject trades. It adjusts the
ranking of an already valid technical candidate.

The comparison uses four completed 15-minute intervals:

BUY:
    stock return minus benchmark return

SELL:
    benchmark return minus stock return

Nifty and sector contributions are each capped at plus or minus
10 ranking points. Missing benchmark data contributes zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import pandas as pd


RELATIVE_STRENGTH_LOOKBACK_BARS = 4
POINTS_PER_PERCENT_EDGE = 10.0
MAX_POINTS_PER_BENCHMARK = 10.0


@dataclass(frozen=True)
class RelativeStrengthAssessment:
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


def completed_return_pct(
    candles: pd.DataFrame,
    as_of,
    lookback_bars: int = (
        RELATIVE_STRENGTH_LOOKBACK_BARS
    ),
) -> float | None:
    """
    Calculate percentage return across completed intervals.

    Four intervals require five completed closing prices.
    """

    if (
        candles is None
        or candles.empty
        or "date" not in candles.columns
        or "close" not in candles.columns
        or lookback_bars < 1
    ):
        return None

    try:
        completed = candles[
            candles["date"] <= as_of
        ].copy()
    except Exception:
        return None

    closes = pd.to_numeric(
        completed["close"],
        errors="coerce",
    ).dropna()

    required_closes = lookback_bars + 1

    if len(closes) < required_closes:
        return None

    starting_close = _number(
        closes.iloc[-required_closes]
    )

    ending_close = _number(
        closes.iloc[-1]
    )

    if (
        starting_close is None
        or ending_close is None
        or starting_close <= 0
    ):
        return None

    return round(
        (
            ending_close
            / starting_close
            - 1.0
        )
        * 100.0,
        6,
    )


def directional_edge_pct(
    direction: str,
    stock_return_pct: float,
    benchmark_return_pct: float,
) -> float:
    edge = (
        stock_return_pct
        - benchmark_return_pct
    )

    if direction == "SELL":
        edge *= -1.0

    return round(edge, 6)


def score_relative_edge(
    edge_pct: float | None,
) -> float:
    if edge_pct is None:
        return 0.0

    score = (
        edge_pct
        * POINTS_PER_PERCENT_EDGE
    )

    score = max(
        -MAX_POINTS_PER_BENCHMARK,
        min(
            MAX_POINTS_PER_BENCHMARK,
            score,
        ),
    )

    return round(score, 2)


def assess_relative_strength(
    signal,
    stock_df_15m: pd.DataFrame,
    market_df_15m: pd.DataFrame,
    sector_df_15m: pd.DataFrame | None = None,
) -> RelativeStrengthAssessment:
    as_of = getattr(
        signal,
        "timestamp",
        None,
    )

    direction = str(
        getattr(
            signal,
            "direction",
            "",
        )
    ).upper()

    stock_return = completed_return_pct(
        stock_df_15m,
        as_of,
    )

    market_return = completed_return_pct(
        market_df_15m,
        as_of,
    )

    sector_return = completed_return_pct(
        sector_df_15m,
        as_of,
    )

    market_edge = None
    sector_edge = None

    if (
        stock_return is not None
        and market_return is not None
    ):
        market_edge = directional_edge_pct(
            direction,
            stock_return,
            market_return,
        )

    if (
        stock_return is not None
        and sector_return is not None
    ):
        sector_edge = directional_edge_pct(
            direction,
            stock_return,
            sector_return,
        )

    market_score = score_relative_edge(
        market_edge
    )

    sector_score = score_relative_edge(
        sector_edge
    )

    total_score = round(
        market_score + sector_score,
        2,
    )

    available_benchmarks = []

    if market_return is not None:
        available_benchmarks.append(
            "NIFTY"
        )

    if sector_return is not None:
        available_benchmarks.append(
            "SECTOR"
        )

    detail = {
        "lookback_bars": (
            RELATIVE_STRENGTH_LOOKBACK_BARS
        ),
        "stock_return_pct": stock_return,
        "market_return_pct": market_return,
        "sector_return_pct": sector_return,
        "market_edge_pct": market_edge,
        "sector_edge_pct": sector_edge,
        "market_score": market_score,
        "sector_score": sector_score,
        "available_benchmarks": (
            available_benchmarks
        ),
    }

    if stock_return is None:
        reason = (
            "stock return unavailable; "
            "ranking unchanged"
        )
    elif not available_benchmarks:
        reason = (
            "benchmark returns unavailable; "
            "ranking unchanged"
        )
    elif total_score > 0:
        reason = (
            "stock outperforms in trade direction"
        )
    elif total_score < 0:
        reason = (
            "stock underperforms in trade direction"
        )
    else:
        reason = (
            "stock matches available benchmarks"
        )

    return RelativeStrengthAssessment(
        score_adjustment=total_score,
        reason=reason,
        detail=detail,
    )
