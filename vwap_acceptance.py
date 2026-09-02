"""VWAP acceptance confirmation for completed entry candles.

This module does not generate a direction. It validates an existing BUY or
SELL candidate by requiring multiple consecutive completed candles to close
on the correct side of VWAP. Missing or invalid data fails closed while the
filter is enabled. When disabled, it never blocks a signal.
"""

import logging
from typing import Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger("vwap_acceptance")

PASS = "PASS"
FAIL = "FAIL"
NOT_ENABLED = "NOT_ENABLED"


def evaluate_vwap_acceptance(
    df_5m: pd.DataFrame,
    direction: str,
    cfg,
) -> Tuple[str, Dict]:
    """Return (status, detail) for an existing BUY or SELL candidate.

    Default behaviour is enabled and requires two consecutive completed
    entry closes on the correct side of each candle's VWAP. The caller is
    responsible for supplying a dataframe whose last row is completed.

    Optional ``VWAP_ACCEPTANCE_REQUIRE_FULL_CANDLE`` additionally requires
    the latest candle's entire range to remain on the accepted side of VWAP:
    low >= VWAP for BUY and high <= VWAP for SELL.
    """
    if not getattr(cfg, "ENABLE_VWAP_ACCEPTANCE_FILTER", True):
        return NOT_ENABLED, {"reason": "VWAP acceptance filter disabled"}

    try:
        bars = int(getattr(cfg, "VWAP_ACCEPTANCE_BARS", 2))
        require_full_candle = bool(
            getattr(cfg, "VWAP_ACCEPTANCE_REQUIRE_FULL_CANDLE", False)
        )

        if direction not in ("BUY", "SELL"):
            return FAIL, {"reason": f"invalid direction: {direction}"}
        if bars < 2:
            return FAIL, {"reason": f"VWAP_ACCEPTANCE_BARS must be >= 2, got {bars}"}
        if df_5m is None or df_5m.empty:
            return FAIL, {"reason": "no entry-candle data"}
        if len(df_5m) < bars:
            return FAIL, {
                "reason": f"insufficient completed candles (have {len(df_5m)}, need {bars})"
            }

        required = {"close", "vwap"}
        if require_full_candle:
            required.add("low" if direction == "BUY" else "high")
        missing = sorted(required.difference(df_5m.columns))
        if missing:
            return FAIL, {"reason": f"missing columns: {', '.join(missing)}"}

        window = df_5m.iloc[-bars:]
        if window[list(required)].isna().any().any():
            return FAIL, {"reason": "NaN in VWAP acceptance inputs"}

        if direction == "BUY":
            close_checks = window["close"] > window["vwap"]
            accepted = bool(close_checks.all())
            full_candle_ok: Optional[bool] = None
            if require_full_candle:
                latest = window.iloc[-1]
                full_candle_ok = bool(latest["low"] >= latest["vwap"])
                accepted = accepted and full_candle_ok
        else:
            close_checks = window["close"] < window["vwap"]
            accepted = bool(close_checks.all())
            full_candle_ok = None
            if require_full_candle:
                latest = window.iloc[-1]
                full_candle_ok = bool(latest["high"] <= latest["vwap"])
                accepted = accepted and full_candle_ok

        detail = {
            "direction": direction,
            "bars_required": bars,
            "closes": [round(float(v), 4) for v in window["close"]],
            "vwaps": [round(float(v), 4) for v in window["vwap"]],
            "close_checks": [bool(v) for v in close_checks],
            "require_full_candle": require_full_candle,
            "full_candle_ok": full_candle_ok,
        }

        if accepted:
            detail["reason"] = f"{bars} completed candles accepted on {direction} side of VWAP"
            return PASS, detail

        detail["reason"] = f"VWAP acceptance not confirmed for {direction}"
        return FAIL, detail

    except Exception as exc:
        logger.exception("VWAP acceptance evaluation failed; rejecting safely")
        return FAIL, {"reason": f"unexpected error: {exc}"}


def format_vwap_acceptance_log(symbol: str, status: str, detail: Dict) -> str:
    return (
        f"{symbol}: VWAP_ACCEPTANCE | status={status} "
        f"| direction={detail.get('direction', 'n/a')} "
        f"| bars={detail.get('bars_required', 'n/a')} "
        f"| closes={detail.get('closes', 'n/a')} "
        f"| vwaps={detail.get('vwaps', 'n/a')} "
        f"| reason={detail.get('reason', 'unknown')}"
    )
