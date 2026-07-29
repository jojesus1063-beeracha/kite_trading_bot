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
from indicators import add_indicators

NIFTY50_TOKEN = 256265  # NSE:NIFTY 50, confirmed via kite.instruments('NSE')

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


def get_market_trend(kite, cfg) -> str:
    """
    Fetches live Nifty 50 15m data and classifies it. Isolated: does
    not touch the trading pipeline, does not affect any signal or
    confidence yet -- proves the live fetch -> classify path works.
    Fails safe to "Sideways" on any error (never raises into a caller
    that might be mid-scan).
    """
    from data_feed import fetch_candles
    try:
        df_15m = fetch_candles(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME, lookback_days=5)
        if df_15m.empty:
            return "Sideways"
        df_15m, _ = add_indicators(df_15m, df_15m.copy(), cfg)
        return classify_trend(df_15m, cfg)
    except Exception:
        return "Sideways"
