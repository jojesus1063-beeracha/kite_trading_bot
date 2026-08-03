"""
Additional entry-location checks.

These checks do not change capital, risk percentage, position
quantity, stop-loss, profit target, maximum open positions,
maximum trades or the daily-loss limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import pandas as pd


MAX_SIGNAL_BODY_ATR = 1.50
MAX_EMA_DISTANCE_ATR = 0.80
MAX_VWAP_DISTANCE_ATR = 2.50


@dataclass(frozen=True)
class EntryQuality:
    accepted: bool
    score: float
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


def _atr(
    candles: pd.DataFrame,
    period: int = 14,
) -> float | None:
    if candles is None or len(candles) < 10:
        return None

    high = pd.to_numeric(
        candles["high"],
        errors="coerce",
    )

    low = pd.to_numeric(
        candles["low"],
        errors="coerce",
    )

    close = pd.to_numeric(
        candles["close"],
        errors="coerce",
    )

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    value = true_range.rolling(
        period,
        min_periods=10,
    ).mean().iloc[-1]

    value = _number(value)

    if value is None or value <= 0:
        return None

    return value


def _session_vwap(
    candles: pd.DataFrame,
) -> float | None:
    if candles is None or candles.empty:
        return None

    if "vwap" in candles.columns:
        existing = _number(
            candles.iloc[-1].get("vwap")
        )

        if existing is not None:
            return existing

    working = candles.copy()

    working["date"] = pd.to_datetime(
        working["date"],
        errors="coerce",
    )

    working = working.dropna(
        subset=["date"],
    )

    if working.empty:
        return None

    latest_session = (
        working["date"].dt.date.iloc[-1]
    )

    session = working[
        working["date"].dt.date
        == latest_session
    ].copy()

    volume = pd.to_numeric(
        session["volume"],
        errors="coerce",
    ).fillna(0)

    total_volume = float(volume.sum())

    if total_volume <= 0:
        return None

    typical_price = (
        pd.to_numeric(
            session["high"],
            errors="coerce",
        )
        + pd.to_numeric(
            session["low"],
            errors="coerce",
        )
        + pd.to_numeric(
            session["close"],
            errors="coerce",
        )
    ) / 3.0

    return _number(
        (typical_price * volume).sum()
        / total_volume
    )


def assess_entry_quality(
    signal,
    df_5m: pd.DataFrame,
) -> EntryQuality:
    """
    Reject only clearly measurable overextension.

    When the required quality data is unavailable, retain the
    existing strategy result rather than manufacturing a rejection.
    """

    required_columns = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ema_entry",
    }

    if (
        df_5m is None
        or len(df_5m) < 10
        or not required_columns.issubset(
            df_5m.columns
        )
    ):
        return EntryQuality(
            accepted=True,
            score=50.0,
            reason=(
                "quality data unavailable; "
                "existing strategy retained"
            ),
            detail={
                "quality_data_available": False,
            },
        )

    latest = df_5m.iloc[-1]

    open_price = _number(
        latest.get("open")
    )

    close = _number(
        latest.get("close")
    )

    ema_entry = _number(
        latest.get("ema_entry")
    )

    atr_value = _atr(df_5m)
    vwap_value = _session_vwap(df_5m)

    if (
        open_price is None
        or close is None
        or ema_entry is None
        or atr_value is None
    ):
        return EntryQuality(
            accepted=True,
            score=50.0,
            reason=(
                "quality values unavailable; "
                "existing strategy retained"
            ),
            detail={
                "quality_data_available": False,
            },
        )

    body_atr = (
        abs(close - open_price)
        / atr_value
    )

    ema_distance_atr = (
        abs(close - ema_entry)
        / atr_value
    )

    vwap_distance_atr = (
        abs(close - vwap_value)
        / atr_value
        if vwap_value is not None
        else None
    )

    reasons: list[str] = []

    if body_atr > MAX_SIGNAL_BODY_ATR:
        reasons.append(
            "signal candle body is overextended"
        )

    if (
        ema_distance_atr
        > MAX_EMA_DISTANCE_ATR
    ):
        reasons.append(
            "entry price is too far from EMA9"
        )

    if (
        vwap_distance_atr is not None
        and vwap_distance_atr
        > MAX_VWAP_DISTANCE_ATR
    ):
        reasons.append(
            "entry price is excessively far from VWAP"
        )

    score = 100.0

    score -= min(
        body_atr * 18.0,
        35.0,
    )

    score -= min(
        ema_distance_atr * 25.0,
        35.0,
    )

    if vwap_distance_atr is not None:
        score -= min(
            vwap_distance_atr * 8.0,
            20.0,
        )

    score = round(
        max(score, 0.0),
        2,
    )

    detail = {
        "quality_data_available": True,
        "atr": round(atr_value, 6),
        "signal_body_atr": round(
            body_atr,
            4,
        ),
        "ema_distance_atr": round(
            ema_distance_atr,
            4,
        ),
        "vwap_distance_atr": (
            round(
                vwap_distance_atr,
                4,
            )
            if vwap_distance_atr is not None
            else None
        ),
        "ema_entry": ema_entry,
        "vwap": vwap_value,
        "signal_close": close,
    }

    return EntryQuality(
        accepted=not reasons,
        score=score,
        reason=(
            "entry location accepted"
            if not reasons
            else "; ".join(reasons)
        ),
        detail=detail,
    )

# FRESH_ENTRY_PRICE_VALIDATION
MAX_ADVERSE_LIVE_SLIPPAGE_PCT = 0.15
MAX_ABSOLUTE_SIGNAL_DRIFT_PCT = 0.35


@dataclass(frozen=True)
class FreshPriceValidation:
    accepted: bool
    signal_price: float | None
    live_price: float | None
    drift_pct: float | None
    adverse_slippage_pct: float | None
    reason: str


def _strict_market_number(
    value: Any,
) -> float | None:
    """
    Accept broker numeric values while rejecting booleans,
    strings, mocks, NaN, infinity and nonpositive prices.
    """

    if isinstance(value, bool):
        return None

    if not isinstance(value, (int, float)):
        return None

    result = float(value)

    if not isfinite(result) or result <= 0:
        return None

    return result


def fetch_live_price(
    kite,
    exchange: str,
    symbol: str,
) -> float | None:
    """
    Fetch the latest market price immediately before sizing
    and order submission.
    """

    instrument = f"{exchange}:{symbol}"

    try:
        response = kite.quote([instrument])
    except Exception:
        return None

    if not isinstance(response, dict):
        return None

    quote = response.get(instrument)

    if not isinstance(quote, dict):
        return None

    return _strict_market_number(
        quote.get("last_price")
    )


def validate_live_price(
    signal,
    live_price: float | None,
) -> FreshPriceValidation:
    """
    Reject a signal when the current quote has moved too far
    from the completed candle's reference price.
    """

    signal_price = _strict_market_number(
        getattr(signal, "entry_price", None)
    )

    if signal_price is None:
        signal_price = _strict_market_number(
            getattr(signal, "price", None)
        )

    if signal_price is None:
        return FreshPriceValidation(
            accepted=False,
            signal_price=None,
            live_price=live_price,
            drift_pct=None,
            adverse_slippage_pct=None,
            reason="invalid signal reference price",
        )

    if live_price is None:
        return FreshPriceValidation(
            accepted=True,
            signal_price=signal_price,
            live_price=None,
            drift_pct=None,
            adverse_slippage_pct=None,
            reason=(
                "fresh quote unavailable; "
                "existing execution path retained"
            ),
        )

    drift_pct = (
        (live_price - signal_price)
        / signal_price
        * 100.0
    )

    direction = str(
        getattr(signal, "direction", "")
    ).upper()

    if direction == "BUY":
        adverse_slippage_pct = drift_pct
    elif direction == "SELL":
        adverse_slippage_pct = -drift_pct
    else:
        return FreshPriceValidation(
            accepted=False,
            signal_price=signal_price,
            live_price=live_price,
            drift_pct=drift_pct,
            adverse_slippage_pct=None,
            reason="invalid signal direction",
        )

    if (
        abs(drift_pct)
        > MAX_ABSOLUTE_SIGNAL_DRIFT_PCT
    ):
        return FreshPriceValidation(
            accepted=False,
            signal_price=signal_price,
            live_price=live_price,
            drift_pct=drift_pct,
            adverse_slippage_pct=adverse_slippage_pct,
            reason=(
                "live price moved more than "
                f"{MAX_ABSOLUTE_SIGNAL_DRIFT_PCT:.2f}% "
                "from the completed signal"
            ),
        )

    if (
        adverse_slippage_pct
        > MAX_ADVERSE_LIVE_SLIPPAGE_PCT
    ):
        return FreshPriceValidation(
            accepted=False,
            signal_price=signal_price,
            live_price=live_price,
            drift_pct=drift_pct,
            adverse_slippage_pct=adverse_slippage_pct,
            reason=(
                "adverse entry slippage exceeds "
                f"{MAX_ADVERSE_LIVE_SLIPPAGE_PCT:.2f}%"
            ),
        )

    return FreshPriceValidation(
        accepted=True,
        signal_price=signal_price,
        live_price=live_price,
        drift_pct=drift_pct,
        adverse_slippage_pct=adverse_slippage_pct,
        reason="fresh live price remains acceptable",
    )
