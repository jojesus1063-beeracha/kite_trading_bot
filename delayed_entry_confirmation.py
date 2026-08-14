"""Paper-entry trend revalidation after a deliberate confirmation delay."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite

import pandas as pd


@dataclass(frozen=True)
class DelayedEntryDecision:
    accepted: bool
    direction: str
    projected_trend: str
    live_price: float | None
    ema9: float | None
    ema21: float | None
    reason: str

    def detail(self) -> dict:
        return asdict(self)


def assess_delayed_entry(
    direction: str,
    completed_entry_candles: pd.DataFrame,
    live_price,
) -> DelayedEntryDecision:
    """Project EMA9/EMA21 with the refreshed quote and fail closed on weakness."""
    side = str(direction or "").upper()
    if side not in {"BUY", "SELL"}:
        return DelayedEntryDecision(
            False, side, "UNKNOWN", None, None, None, "invalid direction"
        )

    try:
        price = float(live_price)
    except (TypeError, ValueError):
        price = None
    if price is None or not isfinite(price) or price <= 0:
        return DelayedEntryDecision(
            False, side, "UNKNOWN", None, None, None,
            "refreshed live price unavailable or invalid",
        )

    if (
        completed_entry_candles is None
        or completed_entry_candles.empty
        or "close" not in completed_entry_candles.columns
    ):
        return DelayedEntryDecision(
            False, side, "UNKNOWN", price, None, None,
            "completed entry-candle history unavailable",
        )

    closes = pd.to_numeric(
        completed_entry_candles["close"], errors="coerce"
    ).dropna()
    if len(closes) < 21:
        return DelayedEntryDecision(
            False, side, "UNKNOWN", price, None, None,
            "fewer than 21 valid completed entry candles",
        )

    projected = pd.concat(
        [closes.reset_index(drop=True), pd.Series([price])],
        ignore_index=True,
    )
    ema9 = float(projected.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(projected.ewm(span=21, adjust=False).mean().iloc[-1])
    trend = "UP" if ema9 > ema21 else "DOWN" if ema9 < ema21 else "NEUTRAL"

    if side == "BUY":
        accepted = trend == "UP" and price >= ema9
        reason = (
            "BUY trend remains upward after delay"
            if accepted
            else "BUY rejected: refreshed price/EMA structure turned adverse"
        )
    else:
        accepted = trend == "DOWN" and price <= ema9
        reason = (
            "SELL trend remains downward after delay"
            if accepted
            else "SELL rejected: refreshed price/EMA structure turned adverse"
        )

    return DelayedEntryDecision(
        accepted, side, trend, price, ema9, ema21, reason
    )
