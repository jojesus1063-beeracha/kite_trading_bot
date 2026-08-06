"""
Relative Volume (RVOL).

RVOL = current candle's volume / average volume over the last N
COMPLETED candles (the average excludes the candle being measured, and
never includes a still-forming candle -- matching the spec's explicit
requirement).

Reuses indicators.average_volume() directly rather than reimplementing
rolling-average logic -- one source of truth for that calculation,
matching the spec's "reuse existing candle cache" / avoid duplicate
logic requirement.
"""

import logging

import pandas as pd

from indicators import average_volume

logger = logging.getLogger("rvol")

NOT_ENABLED = "NOT_ENABLED"
OK = "OK"


def compute_rvol(df_5m: pd.DataFrame, cfg) -> tuple:
    """
    Returns (status, rvol_value_or_None, detail_dict).
    status is NOT_ENABLED or OK. rvol_value is None when status is
    NOT_ENABLED or when it can't be computed (insufficient data) --
    callers must check for None even when status == OK, since a
    same-status contract keeps this consistent with trend_filters.py's
    "always check the value, not just the status" pattern.

    Never raises.
    """
    if not getattr(cfg, "ENABLE_RVOL_FILTER", False):
        return NOT_ENABLED, None, {"reason": "RVOL filter disabled"}

    try:
        lookback = getattr(cfg, "RVOL_LOOKBACK", 20)

        if df_5m is None or df_5m.empty:
            return OK, None, {"reason": "no candle data available"}

        # The average must be computed over COMPLETED candles only,
        # excluding whichever row is being measured as "current". The
        # caller's df_5m is expected to already exclude any
        # still-forming candle (same guarantee every other module in
        # this codebase relies on) -- this function additionally
        # excludes the very last row from the AVERAGE calculation
        # (using it only as the numerator), so a symbol's own huge
        # current-candle volume never inflates its own baseline.
        if len(df_5m) < lookback + 1:
            return OK, None, {"reason": f"insufficient candles for RVOL (have {len(df_5m)}, need {lookback + 1})"}

        current_volume = df_5m["volume"].iloc[-1]
        history_for_average = df_5m.iloc[:-1]  # exclude the current candle from its own baseline
        avg_series = average_volume(history_for_average, lookback)
        avg_volume = avg_series.iloc[-1]

        if pd.isna(current_volume) or pd.isna(avg_volume) or avg_volume == 0:
            return OK, None, {"reason": "current volume or average volume is NaN/zero -- cannot compute RVOL"}

        rvol = float(current_volume) / float(avg_volume)

        if rvol < 1.0:
            label = "weak participation"
        elif rvol < 1.5:
            label = "average"
        elif rvol < 2.0:
            label = "strong institutional participation"
        else:
            label = "very strong"

        detail = {
            "rvol": round(rvol, 4),
            "current_volume": float(current_volume),
            "avg_volume": round(float(avg_volume), 2),
            "lookback": lookback,
            "label": label,
        }
        return OK, rvol, detail

    except Exception as e:
        logger.exception("rvol: compute_rvol failed unexpectedly -- failing safe (None, not a fabricated value)")
        return OK, None, {"reason": f"unexpected error: {e}"}


def passes_rvol_threshold(df_5m: pd.DataFrame, cfg) -> tuple:
    """
    Convenience wrapper for Feature 3 (volume confirmation gate):
    returns (passes: bool, rvol_value_or_None, detail_dict).
    When NOT_ENABLED, passes=True unconditionally (never blocks a
    signal for a disabled feature). When OK but rvol couldn't be
    computed (None), passes=False -- fail closed, don't confirm a
    signal on data we don't actually have.
    """
    status, rvol, detail = compute_rvol(df_5m, cfg)
    if status == NOT_ENABLED:
        return True, None, detail

    if rvol is None:
        return False, None, detail

    threshold = getattr(cfg, "RVOL_THRESHOLD", 1.5)
    passes = rvol >= threshold
    detail["threshold"] = threshold
    detail["passes"] = passes
    return passes, rvol, detail


def format_rvol_log(symbol: str, rvol_value, detail: dict) -> str:
    if rvol_value is None:
        return f"{symbol}: RVOL | value=N/A | reason={detail.get('reason', 'unknown')}"
    return (f"{symbol}: RVOL | value={rvol_value:.2f} | threshold={detail.get('threshold', 'n/a')} "
            f"| passes={detail.get('passes', 'n/a')} | label={detail.get('label', 'n/a')}")
