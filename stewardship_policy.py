"""Stewardship-oriented trading policy helpers.

Pure functions only: no broker calls, no side effects, no order placement.
They exist so safety/quality decisions can be unit-tested independently
from main.py's orchestration.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


_CONFIDENCE_SCORE = {
    "REJECTED": 0,
    "MEDIUM": 50,
    "HIGH": 75,
    "VERY_STRONG": 90,
}

_ALIGNMENT_ADJUSTMENT = {
    "STRONG_ALIGNED": 10,
    "ALIGNED": 5,
    "NEUTRAL": 0,
    "UNKNOWN": 0,
    None: 0,
    "MISALIGNED": -10,
    "STRONG_MISALIGNMENT": -20,
}


def preserve_minimum_rr_target(
    direction: str,
    signal_entry: float,
    signal_target: float,
    fixed_target_pct: float,
) -> float:
    """Apply a fixed-percent target without ever weakening signal R:R."""
    entry = float(signal_entry)
    rr_target = float(signal_target)
    pct = max(0.0, float(fixed_target_pct)) / 100.0

    if direction == "BUY":
        fixed = entry * (1.0 + pct)
        return max(rr_target, fixed)
    if direction == "SELL":
        fixed = entry * (1.0 - pct)
        return min(rr_target, fixed)
    raise ValueError(f"unsupported direction: {direction}")


def entry_quality_score(
    confidence: Optional[str],
    price_action_score: float = 0.0,
    market_alignment: Optional[str] = None,
    news_adjusted_score: Optional[float] = None,
) -> float:
    """Return one bounded 0-100 score from evidence the bot already computes."""
    if news_adjusted_score is None:
        score = _CONFIDENCE_SCORE.get(confidence, 70) + float(price_action_score or 0.0)
    else:
        score = float(news_adjusted_score)

    score += _ALIGNMENT_ADJUSTMENT.get(market_alignment, 0)
    return max(0.0, min(100.0, score))


def two_candle_adverse_confirmation(
    df_5m: pd.DataFrame,
    direction: str,
    entry_price: float,
    entry_time=None,
    confirm_candles: int = 2,
    last_row_is_forming: bool = True,
    ema_period: int = 20,
) -> bool:
    """Confirm a losing trend only after N completed adverse candles.

    BUY requires every confirming close to be below entry and EMA, with
    closes non-improving through the sequence. SELL is the mirror image.
    The real-time monitor includes a forming candle, so it is ignored by
    default. If an EMA column is not already present, it is calculated
    from close prices locally; this keeps the helper usable on raw
    fetch_candles() output without a second broker/data request.
    """
    if confirm_candles <= 0 or ema_period <= 0:
        return False
    if df_5m is None or df_5m.empty or "close" not in df_5m.columns:
        return False

    completed = df_5m.iloc[:-1].copy() if last_row_is_forming else df_5m.copy()
    if completed.empty:
        return False

    if "ema_entry" not in completed.columns:
        completed["ema_entry"] = completed["close"].ewm(span=ema_period, adjust=False).mean()

    if entry_time is not None and "date" in completed.columns:
        try:
            ts = pd.Timestamp(entry_time)
            dates = pd.to_datetime(completed["date"])
            date_tz = getattr(dates.dt, "tz", None)
            if date_tz is not None and ts.tzinfo is None:
                ts = ts.tz_localize(date_tz)
            elif date_tz is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            completed = completed[dates > ts]
        except Exception:
            return False

    if len(completed) < confirm_candles:
        return False

    seq = completed.iloc[-confirm_candles:]
    if seq["ema_entry"].isna().any():
        return False

    entry = float(entry_price)
    closes = [float(v) for v in seq["close"].tolist()]
    emas = [float(v) for v in seq["ema_entry"].tolist()]

    if direction == "BUY":
        losing_side = all(close < entry and close < ema for close, ema in zip(closes, emas))
        non_improving = all(closes[i] <= closes[i - 1] for i in range(1, len(closes)))
        return bool(losing_side and non_improving)

    if direction == "SELL":
        losing_side = all(close > entry and close > ema for close, ema in zip(closes, emas))
        non_improving = all(closes[i] >= closes[i - 1] for i in range(1, len(closes)))
        return bool(losing_side and non_improving)

    return False
