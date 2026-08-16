"""Point-in-time triple-top/bottom policy for the PAPER strategy.

The detector uses only candles that precede the breakout candle.  A signal is
accepted only on a fresh neckline cross with VWAP alignment and at least the
configured relative volume.  It never calls a broker or places an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd


PATTERN_FIXED_EXIT_POLICY = "PATTERN_FIXED"
TRIPLE_TOP = "TRIPLE_TOP"
TRIPLE_BOTTOM = "TRIPLE_BOTTOM"


def is_pattern_fixed_exit(position: Any) -> bool:
    """Return whether a position owns the validated triple-pattern exit plan."""
    return bool(
        isinstance(position, dict)
        and position.get("exit_policy") == PATTERN_FIXED_EXIT_POLICY
    )


@dataclass(frozen=True)
class TriplePatternDecision:
    accepted: bool
    pattern: str | None = None
    direction: str | None = None
    neckline: float | None = None
    volume_ratio: float | None = None
    vwap: float | None = None
    stop_loss_percent: float | None = None
    profit_target_percent: float | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _numbers(values) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)


def _pivot_indices(values: np.ndarray, mode: str, order: int = 2) -> list[int]:
    result = []
    for index in range(order, len(values) - order):
        center = values[index]
        if not np.isfinite(center):
            continue
        neighbors = np.r_[
            values[index - order:index],
            values[index + 1:index + order + 1],
        ]
        if mode == "high" and center >= np.nanmax(neighbors):
            result.append(index)
        elif mode == "low" and center <= np.nanmin(neighbors):
            result.append(index)
    return result


def _separated(indices: list[int], minimum: int = 4) -> bool:
    return len(indices) >= 2 and all(
        right - left >= minimum for left, right in zip(indices, indices[1:])
    )


def _similar(values: list[float], tolerance: float = 0.004) -> bool:
    mean = float(np.mean(values)) if values else 0.0
    return bool(mean and (max(values) - min(values)) / mean <= tolerance)


def _fresh_cross(close: float, prior_close: float, neckline: float, direction: str) -> bool:
    buffer_pct = 0.0005
    if direction == "BUY":
        return bool(
            close > neckline * (1.0 + buffer_pct)
            and prior_close <= neckline * (1.0 + buffer_pct)
        )
    return bool(
        close < neckline * (1.0 - buffer_pct)
        and prior_close >= neckline * (1.0 - buffer_pct)
    )


def _candidate(history: pd.DataFrame, breakout: pd.Series):
    work = history.tail(40).reset_index(drop=True)
    highs = _numbers(work["high"])
    lows = _numbers(work["low"])
    high_pivots = _pivot_indices(highs, "high")
    low_pivots = _pivot_indices(lows, "low")
    close = float(breakout["close"])
    prior_close = float(work.iloc[-1]["close"])

    last_highs = high_pivots[-3:]
    if len(last_highs) == 3 and _separated(last_highs):
        levels = [highs[index] for index in last_highs]
        neckline = float(min(lows[last_highs[0]:last_highs[2] + 1]))
        if _similar(levels) and _fresh_cross(close, prior_close, neckline, "SELL"):
            return TRIPLE_TOP, "SELL", neckline

    last_lows = low_pivots[-3:]
    if len(last_lows) == 3 and _separated(last_lows):
        levels = [lows[index] for index in last_lows]
        neckline = float(max(highs[last_lows[0]:last_lows[2] + 1]))
        if _similar(levels) and _fresh_cross(close, prior_close, neckline, "BUY"):
            return TRIPLE_BOTTOM, "BUY", neckline
    return None


def _fresh_completed(timestamp, cfg_obj, now=None) -> bool:
    try:
        candle_start = pd.Timestamp(timestamp)
        decision_time = pd.Timestamp(now) if now is not None else pd.Timestamp.now(
            tz="Asia/Kolkata"
        )
        if candle_start.tzinfo is None and decision_time.tzinfo is not None:
            candle_start = candle_start.tz_localize(decision_time.tzinfo)
        elif candle_start.tzinfo is not None and decision_time.tzinfo is None:
            decision_time = decision_time.tz_localize(candle_start.tzinfo)
        elif candle_start.tzinfo is not None and decision_time.tzinfo is not None:
            decision_time = decision_time.tz_convert(candle_start.tzinfo)
        timeframe = str(getattr(cfg_obj, "ENTRY_TIMEFRAME", "3minute"))
        digits = "".join(character for character in timeframe if character.isdigit())
        candle_end = candle_start + pd.Timedelta(minutes=int(digits or 3))
        grace = float(
            getattr(cfg_obj, "PAPER_CANDLE_COMPLETION_GRACE_SECONDS", 5.0)
        )
        maximum_age = float(getattr(cfg_obj, "PAPER_CANDLE_MAX_FRESH_SECONDS", 90.0))
        age = (decision_time - candle_end).total_seconds()
        return grace <= age <= maximum_age
    except (TypeError, ValueError, AttributeError):
        return False


def evaluate_confirmed_triple_pattern(
    df_entry: pd.DataFrame,
    cfg_obj,
    *,
    now=None,
) -> TriplePatternDecision:
    """Evaluate the latest completed entry candle without look-ahead."""
    if not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
        return TriplePatternDecision(False, reasons=["PAPER_ONLY"])
    if not bool(getattr(cfg_obj, "PAPER_ENABLE_TRIPLE_PATTERN", True)):
        return TriplePatternDecision(False, reasons=["DISABLED"])

    required = {"high", "low", "close", "volume", "vwap"}
    if df_entry is None or len(df_entry) < 25 or not required.issubset(df_entry.columns):
        return TriplePatternDecision(False, reasons=["INSUFFICIENT_OR_MISSING_DATA"])

    numeric = df_entry.copy()
    for column in required:
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    numeric = numeric.dropna(subset=["high", "low", "close"])
    if len(numeric) < 25:
        return TriplePatternDecision(False, reasons=["INSUFFICIENT_NUMERIC_DATA"])
    latest_timestamp = numeric.iloc[-1].get("date", numeric.index[-1])
    if not _fresh_completed(latest_timestamp, cfg_obj, now=now):
        return TriplePatternDecision(False, reasons=["CANDLE_NOT_COMPLETED_OR_FRESH"])

    history = numeric.iloc[:-1]
    breakout = numeric.iloc[-1]
    found = _candidate(history, breakout)
    if found is None:
        return TriplePatternDecision(False, reasons=["NO_FRESH_TRIPLE_PATTERN_BREAKOUT"])

    pattern, direction, neckline = found
    prior_volume = pd.to_numeric(history["volume"], errors="coerce").tail(20).mean()
    volume = float(breakout["volume"]) if pd.notna(breakout["volume"]) else None
    volume_ratio = None if not prior_volume or volume is None else volume / float(prior_volume)
    minimum_volume = float(
        getattr(cfg_obj, "PAPER_TRIPLE_PATTERN_MIN_VOLUME_RATIO", 1.5)
    )
    vwap = float(breakout["vwap"]) if pd.notna(breakout["vwap"]) else None
    close = float(breakout["close"])
    reasons = []
    if volume_ratio is None or volume_ratio < minimum_volume:
        reasons.append("TRIPLE_PATTERN_VOLUME_BELOW_MINIMUM")
    if vwap is None or (direction == "BUY" and close <= vwap) or (
        direction == "SELL" and close >= vwap
    ):
        reasons.append("TRIPLE_PATTERN_VWAP_NOT_ALIGNED")

    stop_pct = float(getattr(cfg_obj, "PAPER_TRIPLE_PATTERN_STOP_PERCENT", 0.45))
    target_name = (
        "PAPER_TRIPLE_TOP_TARGET_PERCENT"
        if pattern == TRIPLE_TOP
        else "PAPER_TRIPLE_BOTTOM_TARGET_PERCENT"
    )
    target_default = 1.0 if pattern == TRIPLE_TOP else 2.0
    target_pct = float(getattr(cfg_obj, target_name, target_default))
    return TriplePatternDecision(
        accepted=not reasons,
        pattern=pattern,
        direction=direction,
        neckline=neckline,
        volume_ratio=volume_ratio,
        vwap=vwap,
        stop_loss_percent=stop_pct,
        profit_target_percent=target_pct,
        reasons=reasons,
    )
