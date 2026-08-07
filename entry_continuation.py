"""Entry continuation quality gate.

This module is intentionally pure and side-effect free. It does not place
orders or change risk settings. It evaluates whether the latest completed
5-minute candle is a credible continuation after a pullback/retest, and
rejects entries that are already too extended from their nearest intraday
reference (entry EMA or 15-minute VWAP).

The gate is opt-in through ``ENABLE_ENTRY_CONTINUATION_FILTER``. When the
flag is false, callers should treat the result as accepted/inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class EntryContinuationResult:
    accepted: bool
    reason: str
    detail: dict[str, Any]


def _num(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _distance_pct(price: float, reference: float) -> float:
    if reference <= 0:
        return float("inf")
    return abs(price - reference) / reference * 100.0


def assess_entry_continuation(
    df_5m: pd.DataFrame,
    direction: str,
    cfg,
    *,
    vwap_reference: float | None = None,
) -> EntryContinuationResult:
    """Assess pullback/retest + continuation quality on completed 5m bars.

    BUY confirmation requires:
      * the previous bar to retest/touch either EMA or VWAP within tolerance
        and still close on the bullish side of its entry EMA;
      * the current bar to be bullish and close above the previous high;
      * the current close to remain above its entry EMA;
      * the current close not to be excessively extended from the closest
        available EMA/VWAP reference.

    SELL is the exact mirror image.
    """
    if not getattr(cfg, "ENABLE_ENTRY_CONTINUATION_FILTER", False):
        return EntryContinuationResult(True, "entry continuation filter disabled", {"enabled": False})

    required = {"open", "high", "low", "close", "ema_entry"}
    if df_5m is None or len(df_5m) < 2 or not required.issubset(df_5m.columns):
        return EntryContinuationResult(False, "insufficient entry-continuation data", {"enabled": True})

    prev = df_5m.iloc[-2]
    curr = df_5m.iloc[-1]

    values = {
        "prev_high": _num(prev.get("high")),
        "prev_low": _num(prev.get("low")),
        "prev_close": _num(prev.get("close")),
        "prev_ema": _num(prev.get("ema_entry")),
        "curr_open": _num(curr.get("open")),
        "curr_high": _num(curr.get("high")),
        "curr_low": _num(curr.get("low")),
        "curr_close": _num(curr.get("close")),
        "curr_ema": _num(curr.get("ema_entry")),
    }
    if any(v is None for v in values.values()):
        return EntryContinuationResult(False, "entry-continuation candle values unavailable", values)

    direction = str(direction).upper()
    if direction not in {"BUY", "SELL"}:
        return EntryContinuationResult(False, "unsupported direction", {"direction": direction})

    tolerance_pct = float(getattr(cfg, "ENTRY_RETEST_TOLERANCE_PCT", 0.20))
    max_extension_pct = float(getattr(cfg, "ENTRY_MAX_EXTENSION_PCT", 0.60))
    min_body_fraction = float(getattr(cfg, "ENTRY_CONTINUATION_MIN_BODY_FRACTION", 0.20))

    vwap = _num(vwap_reference)

    # Retest is measured against the previous bar because the current bar is
    # the confirmation bar. A tolerance allows a near-touch rather than
    # requiring an exact EMA/VWAP print.
    prev_ema_distance = _distance_pct(values["prev_low"] if direction == "BUY" else values["prev_high"], values["prev_ema"])
    prev_vwap_distance = None
    if vwap is not None and vwap > 0:
        prev_vwap_distance = _distance_pct(values["prev_low"] if direction == "BUY" else values["prev_high"], vwap)

    retest_ema = prev_ema_distance <= tolerance_pct
    retest_vwap = prev_vwap_distance is not None and prev_vwap_distance <= tolerance_pct
    retest_ok = retest_ema or retest_vwap

    curr_range = max(values["curr_high"] - values["curr_low"], 0.0)
    curr_body = abs(values["curr_close"] - values["curr_open"])
    body_fraction = curr_body / curr_range if curr_range > 0 else 0.0
    body_ok = body_fraction >= min_body_fraction

    extension_refs = [values["curr_ema"]]
    if vwap is not None and vwap > 0:
        extension_refs.append(vwap)
    extension_pct = min(_distance_pct(values["curr_close"], ref) for ref in extension_refs)
    extension_ok = extension_pct <= max_extension_pct

    if direction == "BUY":
        prev_held = values["prev_close"] >= values["prev_ema"]
        continuation = values["curr_close"] > values["prev_high"]
        candle_direction_ok = values["curr_close"] > values["curr_open"]
        ema_side_ok = values["curr_close"] > values["curr_ema"]
    else:
        prev_held = values["prev_close"] <= values["prev_ema"]
        continuation = values["curr_close"] < values["prev_low"]
        candle_direction_ok = values["curr_close"] < values["curr_open"]
        ema_side_ok = values["curr_close"] < values["curr_ema"]

    accepted = all((retest_ok, prev_held, continuation, candle_direction_ok, ema_side_ok, body_ok, extension_ok))

    detail = {
        "enabled": True,
        "direction": direction,
        "retest_ok": retest_ok,
        "retest_ema": retest_ema,
        "retest_vwap": retest_vwap,
        "previous_held_ema": prev_held,
        "continuation_break": continuation,
        "candle_direction_ok": candle_direction_ok,
        "ema_side_ok": ema_side_ok,
        "body_fraction": round(body_fraction, 4),
        "body_ok": body_ok,
        "extension_pct": round(extension_pct, 4),
        "extension_ok": extension_ok,
        "retest_tolerance_pct": tolerance_pct,
        "max_extension_pct": max_extension_pct,
        "min_body_fraction": min_body_fraction,
    }

    if accepted:
        return EntryContinuationResult(True, "pullback/retest held and continuation confirmed", detail)

    failed = [
        name
        for name, ok in (
            ("retest", retest_ok),
            ("previous_ema_hold", prev_held),
            ("continuation_break", continuation),
            ("candle_direction", candle_direction_ok),
            ("ema_side", ema_side_ok),
            ("body_strength", body_ok),
            ("overextension", extension_ok),
        )
        if not ok
    ]
    return EntryContinuationResult(False, "entry continuation rejected: " + ", ".join(failed), detail)
