"""Quantitative, no-look-ahead breakout validation for entry candles."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class BreakoutValidation:
    passed: bool
    direction: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def validate_breakout(
    df: pd.DataFrame,
    direction: str,
    *,
    lookback: int = 20,
    volume_period: int = 20,
    minimum_volume_ratio: float = 1.5,
    atr_period: int = 14,
    minimum_atr_multiplier: float = 1.2,
    maximum_atr_multiplier: float = 3.0,
    clv_threshold: float = 0.60,
) -> BreakoutValidation:
    """Validate a directional breakout using structure, volume, ATR and CLV.

    Structural levels and volume SMA use only candles preceding the breakout.
    ATR_14 follows the bot's existing simple rolling-TR definition and includes
    the current candle. All metrics are returned even when one or more gates
    reject the candidate.
    """
    side = str(direction or "").upper()
    metrics: dict[str, Any] = {
        "lookback": lookback,
        "volume_period": volume_period,
        "minimum_volume_ratio": minimum_volume_ratio,
        "atr_period": atr_period,
        "minimum_atr_multiplier": minimum_atr_multiplier,
        "maximum_atr_multiplier": maximum_atr_multiplier,
        "clv_threshold": clv_threshold,
        "n_period_high": None,
        "n_period_low": None,
        "breakout_close": None,
        "breakout_volume": None,
        "sma20_vol": None,
        "volume_ratio": None,
        "true_range": None,
        "atr_14": None,
        "atr_multiplier": None,
        "clv": None,
        "structure_confirmed": False,
        "volume_confirmed": False,
        "volatility_confirmed": False,
        "not_overextended": False,
        "clv_confirmed": False,
    }
    reasons: list[str] = []

    if side not in {"BUY", "SELL"}:
        return BreakoutValidation(False, side, ["INVALID_DIRECTION"], metrics)

    required_columns = {"high", "low", "close", "volume"}
    if df is None or df.empty:
        return BreakoutValidation(False, side, ["MISSING_CANDLE_DATA"], metrics)
    missing = sorted(required_columns.difference(df.columns))
    if missing:
        metrics["missing_columns"] = missing
        return BreakoutValidation(False, side, ["MISSING_OHLCV_COLUMNS"], metrics)

    lookback = max(1, int(lookback))
    volume_period = max(1, int(volume_period))
    atr_period = max(1, int(atr_period))
    minimum_rows = max(lookback + 1, volume_period + 1, atr_period)
    metrics["minimum_rows"] = minimum_rows
    metrics["available_rows"] = len(df)
    if len(df) < minimum_rows:
        return BreakoutValidation(False, side, ["INSUFFICIENT_HISTORY"], metrics)

    numeric = df[["high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    required_tail = numeric.tail(minimum_rows)
    if required_tail.isna().any().any():
        return BreakoutValidation(False, side, ["INVALID_OHLCV_VALUES"], metrics)

    current = numeric.iloc[-1]
    history = numeric.iloc[:-1]
    prior_structure = history.tail(lookback)
    prior_volume = history["volume"].tail(volume_period)

    n_high = _finite(prior_structure["high"].max())
    n_low = _finite(prior_structure["low"].min())
    close = _finite(current["close"])
    volume = _finite(current["volume"])
    volume_sma = _finite(prior_volume.mean())
    volume_ratio = (
        None
        if volume is None or volume_sma is None or volume_sma <= 0
        else volume / volume_sma
    )

    prev_close = numeric["close"].shift(1)
    true_ranges = pd.concat(
        [
            numeric["high"] - numeric["low"],
            (numeric["high"] - prev_close).abs(),
            (numeric["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    current_tr = _finite(true_ranges.iloc[-1])
    atr_14 = _finite(true_ranges.tail(atr_period).mean())
    atr_multiplier = (
        None
        if current_tr is None or atr_14 is None or atr_14 <= 0
        else current_tr / atr_14
    )

    high = _finite(current["high"])
    low = _finite(current["low"])
    candle_range = None if high is None or low is None else high - low
    clv = (
        None
        if close is None or candle_range is None or candle_range <= 0
        else ((close - low) - (high - close)) / candle_range
    )

    structure_confirmed = bool(
        close is not None
        and (
            (side == "BUY" and n_high is not None and close > n_high)
            or (side == "SELL" and n_low is not None and close < n_low)
        )
    )
    volume_confirmed = bool(
        volume_ratio is not None and volume_ratio >= minimum_volume_ratio
    )
    volatility_confirmed = bool(
        atr_multiplier is not None
        and atr_multiplier >= minimum_atr_multiplier
    )
    not_overextended = bool(
        atr_multiplier is not None
        and atr_multiplier <= maximum_atr_multiplier
    )
    clv_confirmed = bool(
        clv is not None
        and (
            (side == "BUY" and clv >= clv_threshold)
            or (side == "SELL" and clv <= -clv_threshold)
        )
    )

    metrics.update({
        "n_period_high": n_high,
        "n_period_low": n_low,
        "breakout_close": close,
        "breakout_volume": volume,
        "sma20_vol": volume_sma,
        "volume_ratio": volume_ratio,
        "true_range": current_tr,
        "atr_14": atr_14,
        "atr_multiplier": atr_multiplier,
        "clv": clv,
        "structure_confirmed": structure_confirmed,
        "volume_confirmed": volume_confirmed,
        "volatility_confirmed": volatility_confirmed,
        "not_overextended": not_overextended,
        "clv_confirmed": clv_confirmed,
    })

    if not structure_confirmed:
        reasons.append("N_PERIOD_EXTREMUM_NOT_BROKEN")
    if not volume_confirmed:
        reasons.append("VOLUME_RATIO_BELOW_MINIMUM")
    if not volatility_confirmed:
        reasons.append("ATR_EXPANSION_BELOW_MINIMUM")
    if not not_overextended:
        reasons.append("BREAKOUT_OVEREXTENDED_ATR")
    if not clv_confirmed:
        reasons.append("CLV_DIRECTION_NOT_CONFIRMED")

    return BreakoutValidation(not reasons, side, reasons, metrics)
