#!/usr/bin/env python3
"""Observation-only Matmon value and exhaustion research.

Nothing in this module authorizes or rejects a trade.  It returns JSON-safe
measurements which the live launcher can persist for end-of-day analysis.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from indicators import atr


def _finite(value: Any, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _last_finite(series):
    values = pd.to_numeric(series, errors="coerce").dropna()
    return None if values.empty else _finite(values.iloc[-1])


def evaluate_shadow(df, direction, plus_di_series, minus_di_series, *, atr_period=14):
    """Return turnover and possible saturation metrics without gating.

    ``would_flag_saturation`` is a research label only.  It requires both an
    extension condition and evidence of weakening/rejection.
    """
    base = {
        "available": False,
        "observation_only": True,
        "would_flag_saturation": False,
        "reasons": [],
    }
    required = {"open", "high", "low", "close", "volume"}
    if df is None or df.empty or not required.issubset(df.columns):
        base["reasons"] = ["INSUFFICIENT_CANDLE_DATA"]
        return base

    clean = df.copy()
    for key in required:
        clean[key] = pd.to_numeric(clean[key], errors="coerce")
    clean = clean.dropna(subset=list(required))
    if clean.empty or direction not in {"BUY", "SELL"}:
        base["reasons"] = ["INVALID_INPUT"]
        return base

    current = clean.iloc[-1]
    close = _finite(current["close"])
    volume = _finite(current["volume"], 0.0)
    atr_value = _last_finite(atr(clean, int(atr_period)))
    if close is None or close <= 0 or atr_value is None or atr_value <= 0:
        base["reasons"] = ["ATR_OR_PRICE_UNAVAILABLE"]
        return base

    candle_values = ((clean["high"] + clean["low"] + clean["close"]) / 3.0) * clean["volume"]
    prior = candle_values.iloc[-21:-1] if len(candle_values) > 1 else candle_values.iloc[0:0]
    prior = prior[pd.notna(prior) & (prior > 0)]
    baseline = _finite(prior.median()) if not prior.empty else None
    current_value = close * max(0.0, volume)
    value_ratio = current_value / baseline if baseline and baseline > 0 else None

    # Use the current session when timestamps are available; otherwise use
    # the supplied history. This is observational and cannot block on errors.
    session = clean
    if "date" in clean.columns:
        dates = pd.to_datetime(clean["date"], errors="coerce")
        if dates.notna().any():
            last_date = dates.dropna().iloc[-1].date()
            selected = clean.loc[dates.dt.date == last_date]
            if not selected.empty:
                session = selected

    session_open = _finite(session.iloc[0]["open"], close)
    session_high = _finite(session["high"].max(), close)
    session_low = _finite(session["low"].min(), close)
    session_turnover = _finite((((session["high"] + session["low"] + session["close"]) / 3.0) * session["volume"]).sum(), 0.0)

    sign = 1.0 if direction == "BUY" else -1.0
    session_move_atr = sign * (close - session_open) / atr_value
    candle_range = max(0.0, _finite(current["high"], close) - _finite(current["low"], close))
    body_atr = abs(close - _finite(current["open"], close)) / atr_value
    ema15 = _finite(pd.to_numeric(clean["close"], errors="coerce").ewm(span=15, adjust=False).mean().iloc[-1], close)
    ema_distance_atr = sign * (close - ema15) / atr_value
    if candle_range > 0:
        adverse_wick_ratio = ((_finite(current["high"], close) - close) / candle_range if direction == "BUY" else (close - _finite(current["low"], close)) / candle_range)
    else:
        adverse_wick_ratio = 0.0

    pdi = pd.to_numeric(plus_di_series, errors="coerce").dropna()
    mdi = pd.to_numeric(minus_di_series, errors="coerce").dropna()
    di_margin = di_margin_previous = di_margin_change = None
    if len(pdi) >= 2 and len(mdi) >= 2:
        current_margin = (pdi.iloc[-1] - mdi.iloc[-1]) if direction == "BUY" else (mdi.iloc[-1] - pdi.iloc[-1])
        previous_margin = (pdi.iloc[-2] - mdi.iloc[-2]) if direction == "BUY" else (mdi.iloc[-2] - pdi.iloc[-2])
        di_margin = _finite(current_margin)
        di_margin_previous = _finite(previous_margin)
        if di_margin is not None and di_margin_previous is not None:
            di_margin_change = di_margin - di_margin_previous

    extension = []
    weakening = []
    if ema_distance_atr >= 2.0:
        extension.append("EMA15_EXTENSION_GE_2_ATR")
    if session_move_atr >= 3.0:
        extension.append("SESSION_MOVE_GE_3_ATR")
    if body_atr >= 1.5:
        extension.append("CANDLE_BODY_GE_1_5_ATR")
    if adverse_wick_ratio >= 0.35:
        weakening.append("ADVERSE_WICK_GE_35PCT")
    if di_margin_change is not None and di_margin_change < 0:
        weakening.append("DI_MARGIN_WEAKENING")

    reasons = extension + weakening
    base.update({
        "available": True,
        "current_rupee_turnover": round(current_value, 2),
        "baseline_rupee_turnover": round(baseline, 2) if baseline is not None else None,
        "turnover_ratio": round(value_ratio, 6) if value_ratio is not None else None,
        "session_rupee_turnover": round(session_turnover, 2),
        "atr": round(atr_value, 6),
        "ema15_distance_atr": round(ema_distance_atr, 6),
        "body_atr": round(body_atr, 6),
        "session_move_atr": round(session_move_atr, 6),
        "adverse_wick_ratio": round(adverse_wick_ratio, 6),
        "di_margin": round(di_margin, 6) if di_margin is not None else None,
        "di_margin_previous": round(di_margin_previous, 6) if di_margin_previous is not None else None,
        "di_margin_change": round(di_margin_change, 6) if di_margin_change is not None else None,
        "extension_reasons": extension,
        "weakening_reasons": weakening,
        "reasons": reasons,
        "would_flag_saturation": bool(extension and weakening),
    })
    return base


def add_preopen_value_shadow(candidates):
    """Attach rupee-turnover percentiles without changing production scores."""
    if not candidates:
        return
    values = []
    for candidate in candidates:
        turnover = max(0.0, _finite(candidate.get("last_price"), 0.0) * _finite(candidate.get("volume"), 0.0))
        candidate["rupee_turnover_shadow"] = round(turnover, 2)
        values.append(turnover)
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0
        for pos in range(i, j + 1):
            ranks[order[pos]] = rank
        i = j + 1
    denominator = max(1, len(values) - 1)
    for index, candidate in enumerate(candidates):
        percentile = ranks[index] / denominator
        candidate["rupee_turnover_percentile_shadow"] = round(percentile, 6)
        candidate["low_value_shadow"] = percentile < 0.20
