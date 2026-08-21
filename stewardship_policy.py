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
    """Apply a fixed-percent target without ever weakening signal R:R.

    `signal_target` is assumed to be the strategy-approved minimum-R:R
    target. A fixed target may extend the reward, but may never pull the
    target closer and silently turn (for example) a 2R trade into 0.9R.
    """
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
    """Return one bounded 0-100 quality score from existing evidence.

    This deliberately does not invent new indicators. It consolidates
    evidence already computed by the bot: ADX confidence, price-action
    score, optional news-adjusted score, and market/sector alignment.
    """
    if news_adjusted_score is None:
        score = _CONFIDENCE_SCORE.get(confidence, 70) + float(price_action_score or 0.0)
    else:
        # main.py's news score is already based on technical + price action.
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
) -> bool:
    """Confirm a losing trend only after N completed adverse candles.

    For BUY: each confirming candle must close below both the entry price
    and its EMA entry line, and closes must not improve across the sequence.
    SELL is the mirror image. This avoids exiting on one noisy red/green bar.

    `last_row_is_forming=True` is the safe default for the real-time
    position monitor, which requests trim_incomplete=False.
    """
    if confirm_candles <= 0:
        return False
    if df_5m is None or df_5m.empty:
        return False

    completed = df_5m.iloc[:-1].copy() if last_row_is_forming else df_5m.copy()
    if completed.empty or "ema_entry" not in completed.columns:
        return False

    if entry_time is not None and "date" in completed.columns:
        try:
            ts = pd.Timestamp(entry_time)
            dates = pd.to_datetime(completed["date"])
            if getattr(dates.dt, "tz", None) is not None and ts.tzinfo is None:
                ts = ts.tz_localize(dates.dt.tz)
            elif getattr(dates.dt, "tz", None) is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            completed = completed[dates > ts]
        except Exception:
            # Time parsing failure must not create a false exit signal.
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
