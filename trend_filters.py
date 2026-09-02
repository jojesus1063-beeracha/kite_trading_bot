"""
Higher-timeframe 200 EMA trend filter.

An OPTIONAL, isolated confirmation layer that can be checked alongside
strategy.evaluate()'s existing entry conditions. This module does not
import or modify strategy.py -- integration is a single additional
condition at the call site (see INTEGRATION.md), not a rewrite of the
existing decision pipeline.

Design contract, per the feature spec:
- Never raises. Any error is caught and reported as FAIL with a
  descriptive reason, never propagated to the caller.
- Uses only already-fetched, already-cached candle data (df_15m, the
  same DataFrame strategy.evaluate() already receives) -- no new API
  calls, no new fetch path, nothing that could add latency beyond the
  EMA calculation itself.
- Only ever completed candles -- the caller's df_15m already excludes
  the still-forming candle (same guarantee candle_engine.py enforces
  elsewhere in this codebase), this module does not re-derive that.
- When ENABLE_200_EMA_FILTER is False (the default), evaluate_200ema_filter()
  returns NOT_ENABLED immediately without touching the DataFrame at all.
"""

import logging

import pandas as pd

from indicators import ema

logger = logging.getLogger("trend_filters")

PASS = "PASS"
FAIL = "FAIL"
NOT_ENABLED = "NOT_ENABLED"


def evaluate_200ema_filter(df_15m: pd.DataFrame, direction: str, cfg) -> tuple:
    """
    Returns (status, detail_dict).
    status is one of PASS / FAIL / NOT_ENABLED.
    direction is "BUY" or "SELL" -- the direction the caller's existing
    trend/EMA/VWAP/ADX checks have already determined; this filter only
    confirms or rejects that already-determined direction against the
    200 EMA, it never proposes a direction of its own.

    detail_dict always contains at least {"reason": str} and, whenever
    computable, {"ema200": float, "close": float, "distance_pct": float,
    "slope": "positive"/"negative"/"flat"}, matching the spec's required
    logging fields.
    """
    if not getattr(cfg, "ENABLE_200_EMA_FILTER", False):
        return NOT_ENABLED, {"reason": "filter disabled"}

    try:
        period = getattr(cfg, "EMA200_PERIOD", 200)
        lookback = getattr(cfg, "EMA200_LOOKBACK", 250)
        slope_lookback = getattr(cfg, "EMA200_SLOPE_LOOKBACK", 5)
        min_distance_pct = getattr(cfg, "EMA200_MIN_DISTANCE_PCT", 0.10)
        allow_touch = getattr(cfg, "EMA200_ALLOW_TOUCH", False)

        if df_15m is None or df_15m.empty:
            return FAIL, {"reason": "no candle data available"}

        min_required = period + slope_lookback
        if len(df_15m) < min_required:
            return FAIL, {
                "reason": f"insufficient candles for EMA{period} (have {len(df_15m)}, need {min_required})"
            }

        ema200_series = ema(df_15m.tail(lookback) if len(df_15m) > lookback else df_15m, period)

        current_ema200 = ema200_series.iloc[-1]
        current_close = df_15m["close"].iloc[-1]

        if pd.isna(current_ema200) or pd.isna(current_close):
            return FAIL, {"reason": "EMA200 or close is NaN -- insufficient warm-up data"}

        if len(ema200_series) <= slope_lookback:
            return FAIL, {"reason": f"insufficient candles for slope lookback ({slope_lookback})"}

        prior_ema200 = ema200_series.iloc[-1 - slope_lookback]
        if pd.isna(prior_ema200):
            return FAIL, {"reason": "prior EMA200 value is NaN -- insufficient slope data"}

        slope_value = current_ema200 - prior_ema200
        if slope_value > 0:
            slope_label = "positive"
        elif slope_value < 0:
            slope_label = "negative"
        else:
            slope_label = "flat"

        distance_pct = (current_close - current_ema200) / current_ema200 * 100

        detail = {
            "ema200": round(float(current_ema200), 4),
            "close": round(float(current_close), 4),
            "distance_pct": round(float(distance_pct), 4),
            "slope": slope_label,
            "direction": direction,
        }

        if not allow_touch and abs(distance_pct) < min_distance_pct:
            detail["reason"] = f"price within {min_distance_pct}% of EMA200 (touching, not confirmed either side)"
            return FAIL, detail

        if direction == "BUY":
            if current_close <= current_ema200:
                detail["reason"] = "price below/at 200 EMA -- BUY blocked"
                return FAIL, detail
            if slope_label != "positive":
                detail["reason"] = f"200 EMA slope is {slope_label}, not positive -- BUY blocked"
                return FAIL, detail
            detail["reason"] = "price above 200 EMA with positive slope -- BUY confirmed"
            return PASS, detail

        if direction == "SELL":
            if current_close >= current_ema200:
                detail["reason"] = "price above/at 200 EMA -- SELL blocked"
                return FAIL, detail
            if slope_label != "negative":
                detail["reason"] = f"200 EMA slope is {slope_label}, not negative -- SELL blocked"
                return FAIL, detail
            detail["reason"] = "price below 200 EMA with negative slope -- SELL confirmed"
            return PASS, detail

        detail["reason"] = f"unrecognized direction {direction!r}"
        return FAIL, detail

    except Exception as e:
        logger.exception("trend_filters: evaluate_200ema_filter failed unexpectedly -- failing safe (FAIL, not PASS)")
        return FAIL, {"reason": f"unexpected error: {e}"}


def format_rejection_log(symbol: str, status: str, detail: dict) -> str:
    """Matches the spec's required logging format. Never called for
    silent rejections -- every FAIL should be logged via this."""
    if status == NOT_ENABLED:
        return f"{symbol}: 200 EMA FILTER | DISABLED"
    parts = [f"{symbol}: 200 EMA FILTER", f"status={status}"]
    if "direction" in detail:
        parts.append(f"direction={detail['direction']}")
    if "ema200" in detail:
        parts.append(f"ema200={detail['ema200']}")
    if "close" in detail:
        parts.append(f"close={detail['close']}")
    if "slope" in detail:
        parts.append(f"slope={detail['slope']}")
    if "distance_pct" in detail:
        parts.append(f"distance={detail['distance_pct']}%")
    parts.append(f"reason={detail.get('reason', 'unknown')}")
    return " | ".join(parts)


def dashboard_display(status: str, detail: dict) -> dict:
    """Returns the fields the spec asks the dashboard to show. Pure
    function, no I/O -- wiring this into configure_app.py's actual
    rendering is a separate, later step."""
    if status == NOT_ENABLED:
        return {"filter": "DISABLED", "trend": None, "distance_pct": None, "slope": None}
    trend = None
    if "close" in detail and "ema200" in detail:
        trend = "Bullish" if detail["close"] > detail["ema200"] else "Bearish"
    return {
        "filter": status,
        "trend": trend,
        "distance_pct": detail.get("distance_pct"),
        "slope": detail.get("slope"),
    }
