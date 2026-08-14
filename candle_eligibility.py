"""Fail-closed PAPER candlestick eligibility and confluence gate.

This module never creates a direction.  It validates a direction selected by
normal EMA9/EMA21 logic against a completed entry candle, TA-Lib patterns,
volume, price action, VWAP, 15-minute EMA200, and ADX strength. Pattern,
volume and price action use a two-of-three confirmation policy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from costs import estimate_trade_cost


@dataclass
class CandleEligibility:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TIER1_PATTERNS = {
    "Engulfing": "CDLENGULFING",
    "Morning_Star": "CDLMORNINGSTAR",
    "Evening_Star": "CDLEVENINGSTAR",
    "3_White_Soldiers": "CDL3WHITESOLDIERS",
    "3_Black_Crows": "CDL3BLACKCROWS",
    "Piercing": "CDLPIERCING",
    "Dark_Cloud_Cover": "CDLDARKCLOUDCOVER",
    "Hammer": "CDLHAMMER",
    "Shooting_Star": "CDLSHOOTINGSTAR",
}

TIER2_PATTERNS = {
    "Marubozu": "CDLMARUBOZU",
    "Inverted_Hammer": "CDLINVERTEDHAMMER",
    "Hanging_Man": "CDLHANGINGMAN",
    "Harami": "CDLHARAMI",
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _last_value(row: pd.Series, names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in row:
            value = _number(row.get(name))
            if value is not None:
                return value
    return None


def _timeframe_minutes(cfg_obj: Any) -> int:
    text = str(getattr(cfg_obj, "ENTRY_TIMEFRAME", "3minute")).lower()
    digits = "".join(character for character in text if character.isdigit())
    return int(digits or 3)


def _fresh_completed_candle(timestamp: Any, cfg_obj: Any, now: Any = None) -> tuple[bool, dict[str, Any]]:
    try:
        candle_start = pd.Timestamp(timestamp)
        current = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata")
        if candle_start.tzinfo is None:
            candle_start = candle_start.tz_localize("Asia/Kolkata")
        else:
            candle_start = candle_start.tz_convert("Asia/Kolkata")
        if current.tzinfo is None:
            current = current.tz_localize("Asia/Kolkata")
        else:
            current = current.tz_convert("Asia/Kolkata")
        candle_close = candle_start + pd.Timedelta(minutes=_timeframe_minutes(cfg_obj))
        age = (current - candle_close).total_seconds()
        max_age = float(getattr(cfg_obj, "PAPER_CANDLE_MAX_FRESH_SECONDS", 90.0))
        grace = float(getattr(cfg_obj, "PAPER_CANDLE_COMPLETION_GRACE_SECONDS", 5.0))
        return grace <= age <= max_age, {
            "candle_start": candle_start.isoformat(),
            "expected_close": candle_close.isoformat(),
            "seconds_after_close": age,
            "min_completion_grace_seconds": grace,
            "max_fresh_seconds": max_age,
        }
    except Exception as exc:
        return False, {"freshness_error": str(exc)}


def _prior_context(close: pd.Series, bullish: bool) -> bool:
    values = pd.to_numeric(close, errors="coerce").dropna().tail(4).tolist()
    if len(values) < 4:
        return False
    prior = values[:-1]
    return (
        prior[0] > prior[1] > prior[2]
        if bullish
        else prior[0] < prior[1] < prior[2]
    )


def _scan_patterns(df: pd.DataFrame, talib_module: Any) -> dict[str, Any]:
    open_ = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
    high = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)

    scores: dict[str, int] = {}
    for name, function_name in {**TIER1_PATTERNS, **TIER2_PATTERNS}.items():
        function = getattr(talib_module, function_name)
        scores[name] = int(function(open_, high, low, close)[-1])

    doji = int(talib_module.CDLDOJI(open_, high, low, close)[-1])
    bullish = [name for name in TIER1_PATTERNS if scores[name] > 0]
    bearish = [name for name in TIER1_PATTERNS if scores[name] < 0]

    # A hammer/shooting-star only counts after a local pullback/rally.  It can
    # therefore confirm resumption of the broader EMA/VWAP/EMA200 trend.
    close_series = df["close"]
    if "Hammer" in bullish and not _prior_context(close_series, bullish=True):
        bullish.remove("Hammer")
    if "Shooting_Star" in bearish and not _prior_context(close_series, bullish=False):
        bearish.remove("Shooting_Star")

    tier2 = {
        name: score
        for name, score in scores.items()
        if name in TIER2_PATTERNS and score != 0
    }
    return {
        "scores": scores,
        "tier1_bullish": bullish,
        "tier1_bearish": bearish,
        "tier2_observational": tier2,
        "doji_observational": doji,
    }


def _cost_aware_movement(
    df_entry: pd.DataFrame,
    direction: str,
    cfg_obj: Any,
) -> tuple[bool, dict[str, Any]]:
    """Check whether one recent true range can cover costs with a buffer."""
    lookback = int(getattr(cfg_obj, "PAPER_COST_MOVE_LOOKBACK", 14))
    if len(df_entry) < lookback + 1:
        return False, {
            "reason": "insufficient movement history",
            "rows": len(df_entry),
            "minimum_rows": lookback + 1,
        }

    close = pd.to_numeric(df_entry["close"], errors="coerce")
    high = pd.to_numeric(df_entry["high"], errors="coerce")
    low = pd.to_numeric(df_entry["low"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    recent_true_range = _number(true_range.tail(lookback).mean())
    entry = _number(close.iloc[-1])
    if entry is None or entry <= 0 or recent_true_range is None or recent_true_range <= 0:
        return False, {
            "reason": "invalid entry or true range",
            "entry": entry,
            "recent_true_range": recent_true_range,
        }

    capital = float(getattr(cfg_obj, "CAPITAL", 5000.0) or 5000.0)
    risk_pct = float(getattr(cfg_obj, "RISK_PER_TRADE_PCT", 0.20))
    stop_pct = float(getattr(cfg_obj, "STOP_LOSS_PERCENT", 0.45))
    sizing_risk_per_share = entry * stop_pct / 100.0
    risk_rupees = capital * risk_pct / 100.0
    quantity = int(risk_rupees / sizing_risk_per_share) if sizing_risk_per_share > 0 else 0
    if quantity <= 0:
        return False, {
            "reason": "no executable quantity",
            "entry": entry,
            "capital": capital,
            "risk_rupees": risk_rupees,
            "sizing_risk_per_share": sizing_risk_per_share,
            "quantity": quantity,
        }

    atr_multiplier = float(
        getattr(cfg_obj, "PAPER_EXPECTED_MOVE_ATR_MULTIPLIER", 1.0)
    )
    expected_move_per_share = recent_true_range * atr_multiplier
    expected_exit = (
        entry + expected_move_per_share
        if direction == "BUY"
        else max(0.01, entry - expected_move_per_share)
    )
    buy_value = quantity * (entry if direction == "BUY" else expected_exit)
    sell_value = quantity * (expected_exit if direction == "BUY" else entry)
    estimated_cost = estimate_trade_cost(buy_value, sell_value)
    expected_gross = expected_move_per_share * quantity
    required_multiple = float(
        getattr(cfg_obj, "PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE", 2.0)
    )
    required_gross = estimated_cost * required_multiple
    break_even_move_pct = estimated_cost / (quantity * entry) * 100.0
    expected_move_pct = expected_move_per_share / entry * 100.0
    accepted = expected_gross >= required_gross

    return accepted, {
        "entry": entry,
        "quantity": quantity,
        "capital": capital,
        "risk_per_trade_pct": risk_pct,
        "stop_loss_percent_for_sizing": stop_pct,
        "recent_true_range": recent_true_range,
        "atr_multiplier": atr_multiplier,
        "expected_move_per_share": expected_move_per_share,
        "expected_move_pct": expected_move_pct,
        "estimated_round_trip_cost": estimated_cost,
        "break_even_move_pct": break_even_move_pct,
        "expected_gross_pnl": expected_gross,
        "required_gross_to_cost_multiple": required_multiple,
        "required_gross_pnl": required_gross,
        "accepted": accepted,
    }


def evaluate_candle_eligibility(
    df_entry: pd.DataFrame,
    df_15m: pd.DataFrame,
    direction: str,
    cfg_obj: Any,
    *,
    now: Any = None,
    talib_module: Any = None,
    price_action_score: float | None = None,
) -> CandleEligibility:
    """Return whether the latest fully completed entry candle is actionable."""
    reasons: list[str] = []
    detail: dict[str, Any] = {"direction": direction, "fail_closed": True}

    if direction not in {"BUY", "SELL"}:
        return CandleEligibility(False, ["INVALID_DIRECTION"], detail)
    if df_entry is None or df_15m is None or df_entry.empty or df_15m.empty:
        return CandleEligibility(False, ["MISSING_CANDLE_DATA"], detail)

    required = {"open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(df_entry.columns))
    if missing:
        detail["missing_columns"] = missing
        return CandleEligibility(False, ["MISSING_OHLCV_COLUMNS"], detail)

    volume_lookback = int(getattr(cfg_obj, "PAPER_CANDLE_VOLUME_LOOKBACK", 20))
    min_rows = max(21, volume_lookback + 1)
    if len(df_entry) < min_rows:
        detail.update({"rows": len(df_entry), "minimum_rows": min_rows})
        return CandleEligibility(False, ["INSUFFICIENT_PATTERN_OR_VOLUME_HISTORY"], detail)

    latest = df_entry.iloc[-1]
    trend = df_15m.iloc[-1]
    timestamp = latest.get("date", latest.name)
    fresh, freshness = _fresh_completed_candle(timestamp, cfg_obj, now=now)
    detail["freshness"] = freshness
    if not fresh:
        reasons.append("CANDLE_NOT_COMPLETED_OR_FRESH")

    numeric = df_entry[list(required)].apply(pd.to_numeric, errors="coerce")
    if numeric.tail(min_rows).isna().any().any():
        reasons.append("INVALID_OHLCV_VALUES")

    adx = _last_value(trend, ("adx", "ADX"))
    min_adx = float(
        getattr(
            cfg_obj,
            "PAPER_BUY_MIN_ADX" if direction == "BUY" else "PAPER_SELL_MIN_ADX",
            25.0 if direction == "BUY" else 20.0,
        )
    )
    detail.update({"adx": adx, "minimum_adx": min_adx})
    if adx is None or adx < min_adx:
        reasons.append("ADX_STRENGTH_BELOW_MINIMUM_OR_UNAVAILABLE")

    prior_volume = pd.to_numeric(df_entry["volume"], errors="coerce").shift(1)
    volume_sma = _number(prior_volume.rolling(volume_lookback).mean().iloc[-1])
    volume = _number(latest.get("volume"))
    volume_ratio = None if not volume_sma or volume is None else volume / volume_sma
    min_ratio = float(getattr(cfg_obj, "PAPER_CANDLE_MIN_VOLUME_RATIO", 1.2))
    detail.update({
        "volume": volume,
        "prior_volume_sma": volume_sma,
        "volume_ratio": volume_ratio,
        "minimum_volume_ratio": min_ratio,
    })
    volume_confirmed = volume_ratio is not None and volume_ratio > min_ratio

    entry_close = _number(latest.get("close"))
    trend_close = _number(trend.get("close"))
    vwap = _last_value(latest, ("vwap", "VWAP"))
    if vwap is None:
        vwap = _last_value(trend, ("vwap", "VWAP"))
    ema200 = _last_value(trend, ("ema200", "ema_200", "EMA200", "EMA_200"))
    ema200_aligned = (
        trend_close is not None
        and ema200 is not None
        and (
            (direction == "BUY" and trend_close > ema200)
            or (direction == "SELL" and trend_close < ema200)
        )
    )
    require_ema200 = bool(
        getattr(cfg_obj, "PAPER_REQUIRE_EMA200_ALIGNMENT", True)
    )
    detail.update({
        "entry_close": entry_close,
        "trend_close": trend_close,
        "vwap": vwap,
        "ema200": ema200,
        "ema200_available": trend_close is not None and ema200 is not None,
        "ema200_aligned": ema200_aligned,
        "ema200_alignment_required": require_ema200,
    })
    if entry_close is None or vwap is None or (
        direction == "BUY" and entry_close <= vwap
    ) or (
        direction == "SELL" and entry_close >= vwap
    ):
        reasons.append("VWAP_DIRECTION_NOT_ACCEPTED_OR_UNAVAILABLE")
    if require_ema200 and not ema200_aligned:
        reasons.append("EMA200_DIRECTION_NOT_ACCEPTED_OR_UNAVAILABLE")

    if bool(getattr(cfg_obj, "PAPER_ENABLE_COST_AWARE_GATE", False)):
        movement_ok, movement_detail = _cost_aware_movement(
            df_entry, direction, cfg_obj
        )
        detail["cost_aware_movement"] = movement_detail
        if not movement_ok:
            reasons.append("EXPECTED_MOVE_DOES_NOT_COVER_COSTS")

    pattern_confirmed = False
    if talib_module is None:
        try:
            import talib as talib_module  # type: ignore[no-redef]
        except Exception as exc:
            detail["talib_error"] = str(exc)
            reasons.append("TALIB_UNAVAILABLE")

    if talib_module is not None and "INVALID_OHLCV_VALUES" not in reasons:
        try:
            patterns = _scan_patterns(df_entry, talib_module)
            detail["patterns"] = patterns
            bullish = patterns["tier1_bullish"]
            bearish = patterns["tier1_bearish"]
            if bullish and bearish:
                reasons.append("CONFLICTING_TIER1_PATTERNS")
            else:
                pattern_confirmed = bool(
                    bullish if direction == "BUY" else bearish
                )
        except Exception as exc:
            detail["pattern_error"] = str(exc)
            reasons.append("PATTERN_ENGINE_FAILED")

    if price_action_score is None:
        try:
            from price_action import evaluate_price_action
            price_action_score, price_action_detail = evaluate_price_action(
                df_entry, direction, cfg_obj
            )
            detail["price_action"] = price_action_detail
        except Exception as exc:
            detail["price_action_error"] = str(exc)
            reasons.append("PRICE_ACTION_ENGINE_FAILED")

    price_action_value = _number(price_action_score)
    price_action_confirmed = (
        price_action_value is not None and price_action_value > 0.0
    )
    confirmations = {
        "tier1_pattern": pattern_confirmed,
        "volume_above_prior_sma": volume_confirmed,
        "positive_price_action": price_action_confirmed,
    }
    confirmation_count = sum(bool(value) for value in confirmations.values())
    required_confirmations = int(
        getattr(cfg_obj, "PAPER_CANDLE_REQUIRED_CONFIRMATIONS", 2)
    )
    detail.update({
        "price_action_score": price_action_value,
        "confirmations": confirmations,
        "confirmation_count": confirmation_count,
        "required_confirmations": required_confirmations,
    })
    if confirmation_count < required_confirmations:
        reasons.append("INSUFFICIENT_ENTRY_CONFIRMATIONS")

    return CandleEligibility(not reasons, reasons, detail)
