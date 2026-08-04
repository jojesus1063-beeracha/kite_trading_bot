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


_LAST_MARKET_CANDLES = pd.DataFrame()
_LAST_SECTOR_CANDLES: dict[str, pd.DataFrame] = {}


def clear_relative_strength_cache() -> None:
    """
    Clear benchmark snapshots at the beginning of each scan.
    """

    global _LAST_MARKET_CANDLES

    _LAST_MARKET_CANDLES = pd.DataFrame()
    _LAST_SECTOR_CANDLES.clear()


def get_cached_market_candles() -> pd.DataFrame:
    return _LAST_MARKET_CANDLES.copy()


def get_cached_sector_candles(
    sector_name: str | None,
) -> pd.DataFrame:
    if sector_name is None:
        return pd.DataFrame()

    candles = _LAST_SECTOR_CANDLES.get(
        sector_name
    )

    if candles is None:
        return pd.DataFrame()

    return candles.copy()


def _fetch_classified_context_diagnostic(
    kite,
    token,
    cfg,
) -> tuple[str, pd.DataFrame, str]:
    """
    Fetch, enrich and classify one benchmark with a reason code.

    The trend remains fail-safe and backward compatible: every failure
    still resolves to ``Sideways`` with empty candles.  The third return
    value only explains why that fallback was used.
    """

    from data_feed import fetch_candles

    try:
        df_15m = fetch_candles(
            kite,
            token,
            cfg.TREND_TIMEFRAME,
            lookback_days=5,
        )
    except Exception:
        return "Sideways", pd.DataFrame(), "FETCH_ERROR"

    if not isinstance(df_15m, pd.DataFrame) or df_15m.empty:
        return "Sideways", pd.DataFrame(), "EMPTY_DATA"

    try:
        df_15m, _ = add_indicators(
            df_15m,
            df_15m.copy(),
            cfg,
        )

        return (
            classify_trend(
                df_15m,
                cfg,
            ),
            df_15m,
            "OK",
        )
    except Exception:
        return "Sideways", pd.DataFrame(), "INDICATOR_ERROR"


def _fetch_classified_context(
    kite,
    token,
    cfg,
) -> tuple[str, pd.DataFrame]:
    """Backward-compatible context wrapper without diagnostics."""

    trend, candles, _ = _fetch_classified_context_diagnostic(
        kite,
        token,
        cfg,
    )

    return trend, candles


def _fetch_and_classify(
    kite,
    token,
    cfg,
) -> str:
    """
    Backward-compatible trend-only wrapper.
    """

    trend, _ = _fetch_classified_context(
        kite,
        token,
        cfg,
    )

    return trend


def get_market_trend_diagnostic(
    kite,
    cfg,
) -> tuple[str, str]:
    """
    Return Nifty 50 trend plus its diagnostic reason.
    """

    global _LAST_MARKET_CANDLES

    trend, candles, reason = _fetch_classified_context_diagnostic(
        kite,
        NIFTY50_TOKEN,
        cfg,
    )

    _LAST_MARKET_CANDLES = candles

    return trend, reason


def get_market_trend(kite, cfg) -> str:
    """
    Backward-compatible Nifty trend-only wrapper.
    """

    trend, _ = get_market_trend_diagnostic(
        kite,
        cfg,
    )

    return trend


def sector_for_symbol(symbol: str):
    """Returns the sector index tradingsymbol for a watchlist symbol, or None if unmapped."""
    return SECTOR_MAP.get(symbol)


def get_sector_trend_diagnostic(
    kite,
    symbol: str,
    cfg,
) -> tuple[str, str]:
    """
    Return a sector trend plus its diagnostic reason.

    Its trend value deliberately preserves the historical public
    behavior: unmapped and unavailable sectors remain ``Sideways``.
    ``main.py`` separately keeps its established ``UNKNOWN`` treatment
    for unmapped symbols when calculating market alignment.
    """

    sector_name = sector_for_symbol(
        symbol
    )

    if sector_name is None:
        return "Sideways", "UNMAPPED"

    token = SECTOR_INDEX_TOKENS.get(
        sector_name
    )

    if token is None:
        _LAST_SECTOR_CANDLES[
            sector_name
        ] = pd.DataFrame()

        return "Sideways", "MISSING_TOKEN"

    trend, candles, reason = _fetch_classified_context_diagnostic(
        kite,
        token,
        cfg,
    )

    _LAST_SECTOR_CANDLES[
        sector_name
    ] = candles

    return trend, reason


def get_sector_trend(
    kite,
    symbol: str,
    cfg,
) -> str:
    """Backward-compatible sector trend-only wrapper."""

    trend, _ = get_sector_trend_diagnostic(
        kite,
        symbol,
        cfg,
    )

    return trend


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
