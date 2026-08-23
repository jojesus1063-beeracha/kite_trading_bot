"""Point-in-time triple-top/bottom policy for guarded paper/live strategies.

The detector uses only completed candles.  Pattern shape determines whether a
signal is accepted; neckline, VWAP and relative-volume confirmation are kept as
observations for audit/ranking and never block an otherwise valid pattern.
It never calls a broker or places an order.
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
    observations: dict[str, Any] = field(default_factory=dict)

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


def _candidate(history: pd.DataFrame, signal_candle: pd.Series):
    work = history.tail(40).reset_index(drop=True)
    highs = _numbers(work["high"])
    lows = _numbers(work["low"])
    high_pivots = _pivot_indices(highs, "high")
    low_pivots = _pivot_indices(lows, "low")
    close = float(signal_candle["close"])
    prior_close = float(work.iloc[-1]["close"])
    candidates = []

    last_highs = high_pivots[-3:]
    if len(last_highs) == 3 and _separated(last_highs):
        levels = [highs[index] for index in last_highs]
        neckline = float(min(lows[last_highs[0]:last_highs[2] + 1]))
        if _similar(levels):
            candidates.append((
                TRIPLE_TOP,
                "SELL",
                neckline,
                _fresh_cross(close, prior_close, neckline, "SELL"),
                last_highs[-1],
            ))

    last_lows = low_pivots[-3:]
    if len(last_lows) == 3 and _separated(last_lows):
        levels = [lows[index] for index in last_lows]
        neckline = float(max(highs[last_lows[0]:last_lows[2] + 1]))
        if _similar(levels):
            candidates.append((
                TRIPLE_BOTTOM,
                "BUY",
                neckline,
                _fresh_cross(close, prior_close, neckline, "BUY"),
                last_lows[-1],
            ))
    if not candidates:
        return None
    # A currently confirmed shape wins; otherwise observe the most recently
    # completed shape. This keeps confirmation non-blocking without letting an
    # older opposite formation silently override the latest structure.
    pattern, direction, neckline, fresh_cross, _ = max(
        candidates, key=lambda item: (item[3], item[4])
    )
    return pattern, direction, neckline, fresh_cross


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
    allow_live: bool = False,
) -> TriplePatternDecision:
    """Evaluate the latest completed entry candle without look-ahead."""
    paper_mode = bool(getattr(cfg_obj, "PAPER_TRADING", False))
    if allow_live and paper_mode:
        return TriplePatternDecision(False, reasons=["LIVE_MODE_REQUIRED"])
    if not paper_mode and not allow_live:
        return TriplePatternDecision(False, reasons=["PAPER_ONLY"])
    if not bool(getattr(cfg_obj, "PAPER_ENABLE_TRIPLE_PATTERN", True)):
        return TriplePatternDecision(False, reasons=["DISABLED"])

    required = {"high", "low", "close"}
    if df_entry is None or len(df_entry) < 25 or not required.issubset(df_entry.columns):
        return TriplePatternDecision(False, reasons=["INSUFFICIENT_OR_MISSING_DATA"])

    numeric = df_entry.copy()
    for column in required | ({"volume", "vwap"} & set(numeric.columns)):
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    numeric = numeric.dropna(subset=["high", "low", "close"])
    if len(numeric) < 25:
        return TriplePatternDecision(False, reasons=["INSUFFICIENT_NUMERIC_DATA"])
    latest_timestamp = numeric.iloc[-1].get("date", numeric.index[-1])
    if not _fresh_completed(latest_timestamp, cfg_obj, now=now):
        return TriplePatternDecision(False, reasons=["CANDLE_NOT_COMPLETED_OR_FRESH"])

    history = numeric.iloc[:-1]
    signal_candle = numeric.iloc[-1]
    found = _candidate(history, signal_candle)
    if found is None:
        return TriplePatternDecision(False, reasons=["NO_TRIPLE_PATTERN"])

    pattern, direction, neckline, fresh_neckline_cross = found
    prior_volume = (
        pd.to_numeric(history["volume"], errors="coerce").tail(20).mean()
        if "volume" in history.columns
        else None
    )
    volume_value = signal_candle.get("volume")
    volume = float(volume_value) if pd.notna(volume_value) else None
    volume_ratio = None if not prior_volume or volume is None else volume / float(prior_volume)
    minimum_volume = float(
        getattr(cfg_obj, "PAPER_TRIPLE_PATTERN_MIN_VOLUME_RATIO", 1.5)
    )
    vwap_value = signal_candle.get("vwap")
    vwap = float(vwap_value) if pd.notna(vwap_value) else None
    close = float(signal_candle["close"])
    volume_confirmed = volume_ratio is not None and volume_ratio >= minimum_volume
    vwap_aligned = vwap is not None and (
        (direction == "BUY" and close > vwap)
        or (direction == "SELL" and close < vwap)
    )
    observations = {
        "policy": "OBSERVATIONAL_ONLY",
        "fresh_neckline_cross": fresh_neckline_cross,
        "vwap_aligned": vwap_aligned,
        "volume_confirmed": volume_confirmed,
        "minimum_volume_ratio": minimum_volume,
        "failed": [
            name
            for name, passed in (
                ("FRESH_NECKLINE_CROSS", fresh_neckline_cross),
                ("VWAP_ALIGNMENT", vwap_aligned),
                ("VOLUME_CONFIRMATION", volume_confirmed),
            )
            if not passed
        ],
    }

    stop_pct = float(getattr(cfg_obj, "PAPER_TRIPLE_PATTERN_STOP_PERCENT", 0.45))
    target_name = (
        "PAPER_TRIPLE_TOP_TARGET_PERCENT"
        if pattern == TRIPLE_TOP
        else "PAPER_TRIPLE_BOTTOM_TARGET_PERCENT"
    )
    target_default = 1.0 if pattern == TRIPLE_TOP else 2.0
    target_pct = float(getattr(cfg_obj, target_name, target_default))
    return TriplePatternDecision(
        accepted=True,
        pattern=pattern,
        direction=direction,
        neckline=neckline,
        volume_ratio=volume_ratio,
        vwap=vwap,
        stop_loss_percent=stop_pct,
        profit_target_percent=target_pct,
        observations=observations,
    )
