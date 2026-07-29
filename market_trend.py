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

# Sector index tradingsymbol -> instrument token. Confirmed via a real
# kite.instruments('NSE') query (see project history). Hardcoded
# rather than re-fetched each time -- these tokens are stable.
SECTOR_INDEX_TOKENS = {
    "NIFTY BANK": 260105, "NIFTY IT": 259849, "NIFTY AUTO": 263433,
    "NIFTY PHARMA": 262409, "NIFTY METAL": 263689, "NIFTY ENERGY": 261641,
    "NIFTY FMCG": 261897, "NIFTY REALTY": 261129, "NIFTY MEDIA": 263945,
    "NIFTY PSU BANK": 262921, "NIFTY FIN SERVICE": 257801, "NIFTY INFRA": 261385,
}

# Watchlist symbol -> sector index tradingsymbol. Manually maintained --
# Kite does not provide this mapping. Symbols not listed here have no
# sector-level trend (get_sector_trend falls back to "Sideways").
SECTOR_MAP = {
    "HDFCBANK": "NIFTY BANK", "ICICIBANK": "NIFTY BANK", "AXISBANK": "NIFTY BANK",
    "KOTAKBANK": "NIFTY BANK", "SBIN": "NIFTY BANK", "INDUSINDBK": "NIFTY BANK",
    "BANDHANBNK": "NIFTY BANK", "IDFCFIRSTB": "NIFTY BANK",
    "PNB": "NIFTY PSU BANK", "CANBK": "NIFTY PSU BANK", "UNIONBANK": "NIFTY PSU BANK",
    "TCS": "NIFTY IT", "INFY": "NIFTY IT", "HCLTECH": "NIFTY IT", "WIPRO": "NIFTY IT",
    "TECHM": "NIFTY IT",
    "MARUTI": "NIFTY AUTO", "TVSMOTOR": "NIFTY AUTO", "EICHERMOT": "NIFTY AUTO",
    "HEROMOTOCO": "NIFTY AUTO", "M&M": "NIFTY AUTO", "BAJAJ-AUTO": "NIFTY AUTO",
    "SUNPHARMA": "NIFTY PHARMA", "DRREDDY": "NIFTY PHARMA", "CIPLA": "NIFTY PHARMA",
    "TATASTEEL": "NIFTY METAL", "JSWSTEEL": "NIFTY METAL",
    "ONGC": "NIFTY ENERGY", "NTPC": "NIFTY ENERGY", "POWERGRID": "NIFTY ENERGY",
    "ITC": "NIFTY FMCG", "HINDUNILVR": "NIFTY FMCG", "NESTLEIND": "NIFTY FMCG",
    "BRIGADE": "NIFTY REALTY",
    "BAJFINANCE": "NIFTY FIN SERVICE", "BAJAJFINSV": "NIFTY FIN SERVICE",
    "SHRIRAMFIN": "NIFTY FIN SERVICE", "IREDA": "NIFTY FIN SERVICE",
    "IRFC": "NIFTY FIN SERVICE", "HUDCO": "NIFTY FIN SERVICE",
    "LT": "NIFTY INFRA", "NBCC": "NIFTY INFRA", "NCC": "NIFTY INFRA",
}

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
    trend = get_trend(latest, cfg, require_vwap=False)  # indices have no real volume, VWAP is always NaN
    return TREND_LABELS[trend]


def _fetch_and_classify(kite, token, cfg) -> str:
    """Shared fetch->indicators->classify path. Fails safe to Sideways."""
    from data_feed import fetch_candles
    try:
        df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
        if df_15m.empty:
            return "Sideways"
        df_15m, _ = add_indicators(df_15m, df_15m.copy(), cfg)
        return classify_trend(df_15m, cfg)
    except Exception:
        return "Sideways"


def get_market_trend(kite, cfg) -> str:
    """
    Fetches live Nifty 50 15m data and classifies it. Isolated: does
    not touch the trading pipeline, does not affect any signal or
    confidence yet -- proves the live fetch -> classify path works.
    Fails safe to "Sideways" on any error (never raises into a caller
    that might be mid-scan).
    """
    return _fetch_and_classify(kite, NIFTY50_TOKEN, cfg)


def sector_for_symbol(symbol: str):
    """Returns the sector index tradingsymbol for a watchlist symbol, or None if unmapped."""
    return SECTOR_MAP.get(symbol)


def get_sector_trend(kite, symbol: str, cfg) -> str:
    """
    Fetches the live sector-index trend for `symbol`'s mapped sector.
    Falls back to "Sideways" if the symbol has no sector mapping, or
    on any fetch error -- same fail-safe philosophy as get_market_trend.
    Still isolated: not wired into the trading pipeline yet.
    """
    sector_name = sector_for_symbol(symbol)
    if sector_name is None:
        return "Sideways"
    token = SECTOR_INDEX_TOKENS.get(sector_name)
    if token is None:
        return "Sideways"
    return _fetch_and_classify(kite, token, cfg)


def compute_market_alignment(direction: str, market_trend: str, sector_trend: str) -> str:
    """
    Pure function: compares the trade's direction against market and
    sector trend, returns "ALIGNED" / "NEUTRAL" / "OPPOSED".

    ALIGNED: both market AND sector trend match the trade direction
             (Bullish for BUY, Bearish for SELL).
    OPPOSED: EITHER market OR sector trend actively contradicts the
             trade direction (the stronger of two negative signals wins
             -- any real opposition is treated as OPPOSED, not averaged
             away by a Sideways reading elsewhere).
    NEUTRAL: everything else (e.g. one Sideways + one matching, or
             both Sideways) -- no strong signal either way.

    Used to adjust Signal.confidence -- never to block a trade outright
    (that decision belongs to the caller, per the "confidence not
    filter" design principle).
    """
    wanted = "Bullish" if direction == "BUY" else "Bearish"
    opposed_label = "Bearish" if direction == "BUY" else "Bullish"

    if market_trend == opposed_label or sector_trend == opposed_label:
        return "OPPOSED"
    if market_trend == wanted and sector_trend == wanted:
        return "ALIGNED"
    return "NEUTRAL"


def compute_market_alignment(direction: str, market_trend: str, sector_trend: str) -> str:
    """
    Pure scoring: how well does a signal's direction align with the
    broader market and sector trend? Additive, -2..+2 scale, mapped to
    a label. Separate from ADX confidence -- never merged with it.

    +1 per trend that agrees with the signal's direction, -1 per trend
    that opposes it, 0 for Sideways.
    """
    wanted = "Bullish" if direction == "BUY" else "Bearish"
    opposed = "Bearish" if direction == "BUY" else "Bullish"

    def score_one(trend):
        if trend == wanted:
            return 1
        if trend == opposed:
            return -1
        return 0

    total = score_one(market_trend) + score_one(sector_trend)

    if total == 2:
        return "STRONG_ALIGNMENT"
    if total == 1:
        return "ALIGNED"
    if total == 0:
        return "NEUTRAL"
    if total == -1:
        return "MISALIGNED"
    return "STRONG_MISALIGNMENT"
