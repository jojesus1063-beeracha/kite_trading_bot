"""
Market/sector/stock trend classification -- pure, dependency-light.

Reuses strategy.get_trend()'s existing, tested EMA/VWAP/ADX logic --
applied to any 15m OHLC DataFrame (Nifty 50, a sector index, or a
stock). This module does not modify get_trend() and does not perform
any Kite API calls, confidence scoring, or caching -- classification
only. Live wiring and sector mapping are separate, later steps.
"""
from typing import Optional
import pandas as pd

from strategy import get_trend

TREND_LABELS = {"UP": "Bullish", "DOWN": "Bearish", None: "Sideways"}


def classify_trend(df_15m: pd.DataFrame, cfg=None) -> str:
    """
    Returns "Bullish" / "Bearish" / "Sideways" for the given 15m OHLC
    DataFrame, using the latest row. Reuses get_trend() exactly as-is:
    "UP" -> Bullish, "DOWN" -> Bearish, None -> Sideways.
    """
    if df_15m is None or df_15m.empty:
        return "Sideways"
    latest = df_15m.iloc[-1]
    trend = get_trend(latest, cfg)
    return TREND_LABELS[trend]
