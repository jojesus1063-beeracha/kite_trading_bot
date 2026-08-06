"""
Dynamic watchlist classification via 200 EMA.

DISTINCT FROM trend_filters.py's evaluate_200ema_filter(): that module
runs AT ENTRY TIME, after EMA20/50/VWAP/ADX have already determined a
direction, and can REJECT an otherwise-valid signal. This module runs
BEFORE any of that -- it decides whether a symbol is even eligible to
be scanned for BUY or SELL at all, using only price vs. the 200 EMA.

Per the spec: "The 200 EMA should NOT be used as a hard trade entry
filter... it should determine which stocks deserve to remain in
today's active watchlist." If a symbol is below its 200 EMA, this
module says "don't even bother computing EMA20/50/VWAP/ADX for a BUY
on this symbol today" -- saving computation, not rejecting a specific
signal that was otherwise fully qualified.

IMPORTANT OPEN QUESTION, not resolved by this module: running this
ALONGSIDE trend_filters.py's entry-level filter means the same 200 EMA
relationship gets checked twice, in two different places, potentially
with different EMA200_LOOKBACK/EMA200_PERIOD config values if they're
ever set differently. Decide deliberately whether both should be
enabled together before turning this on in the same session as the
entry-level filter.

Design contract, matching trend_filters.py's established pattern:
- Never raises.
- Uses only already-fetched candle data (df_15m) -- no new API calls.
- Only completed candles.
- NOT_ENABLED is a true no-op when ENABLE_EMA200_WATCHLIST is False.
"""

import logging

import pandas as pd

from indicators import ema

logger = logging.getLogger("watchlist_filters")

BUY = "BUY"
SELL = "SELL"
NEITHER = "NEITHER"
NOT_ENABLED = "NOT_ENABLED"


def classify_direction_eligibility(df_15m: pd.DataFrame, cfg) -> tuple:
    """
    Returns (eligibility, detail_dict).
    eligibility is one of BUY / SELL / NEITHER / NOT_ENABLED.

    BUY  -- close > EMA200, symbol eligible for BUY-side scanning today
    SELL -- close < EMA200, symbol eligible for SELL-side scanning today
    NEITHER -- insufficient data, or price exactly at EMA200 (rare)
    NOT_ENABLED -- ENABLE_EMA200_WATCHLIST is False, df never touched

    Caller's responsibility (main.py, not this module): skip attempting
    a BUY evaluation entirely if eligibility != BUY, skip SELL entirely
    if eligibility != SELL. NEITHER means skip both directions for
    today. NOT_ENABLED means proceed exactly as before this filter
    existed -- both directions remain in play, decided by the existing
    EMA20/50/VWAP/ADX/price-action logic alone.
    """
    if not getattr(cfg, "ENABLE_EMA200_WATCHLIST", False):
        return NOT_ENABLED, {"reason": "watchlist filter disabled"}

    try:
        period = getattr(cfg, "EMA200_PERIOD", 200)

        if df_15m is None or df_15m.empty:
            return NEITHER, {"reason": "no candle data available"}

        if len(df_15m) < period:
            return NEITHER, {"reason": f"insufficient candles for EMA{period} (have {len(df_15m)}, need {period})"}

        ema200_series = ema(df_15m, period)
        current_ema200 = ema200_series.iloc[-1]
        current_close = df_15m["close"].iloc[-1]

        if pd.isna(current_ema200) or pd.isna(current_close):
            return NEITHER, {"reason": "EMA200 or close is NaN -- insufficient warm-up data"}

        detail = {
            "ema200": round(float(current_ema200), 4),
            "close": round(float(current_close), 4),
        }

        if current_close > current_ema200:
            detail["reason"] = "price above 200 EMA -- BUY watchlist eligible"
            return BUY, detail
        elif current_close < current_ema200:
            detail["reason"] = "price below 200 EMA -- SELL watchlist eligible"
            return SELL, detail
        else:
            detail["reason"] = "price exactly at 200 EMA -- neither side qualified"
            return NEITHER, detail

    except Exception as e:
        logger.exception("watchlist_filters: classify_direction_eligibility failed unexpectedly -- "
                          "failing safe (NEITHER, not BUY/SELL)")
        return NEITHER, {"reason": f"unexpected error: {e}"}


def format_watchlist_log(symbol: str, eligibility: str, detail: dict) -> str:
    if eligibility == NOT_ENABLED:
        return f"{symbol}: EMA200 WATCHLIST | DISABLED"
    parts = [f"{symbol}: EMA200 WATCHLIST", f"eligibility={eligibility}"]
    if "ema200" in detail:
        parts.append(f"ema200={detail['ema200']}")
    if "close" in detail:
        parts.append(f"close={detail['close']}")
    parts.append(f"reason={detail.get('reason', 'unknown')}")
    return " | ".join(parts)
