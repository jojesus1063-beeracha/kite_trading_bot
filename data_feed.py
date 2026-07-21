"""
Historical candle data via Kite Connect's REST API.

For the live loop, main.py polls historical_data() for the most recent
candles every few minutes rather than aggregating ticks from the
WebSocket — this is simpler and plenty fast enough for a 5-min-entry
strategy. If you later want tick-level precision (e.g. faster stop-loss
reaction), swap this for KiteTicker and aggregate candles yourself.
"""

from datetime import datetime, timedelta

import pandas as pd


def get_instrument_token(kite, symbol: str, exchange: str) -> int:
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst["tradingsymbol"] == symbol:
            return inst["instrument_token"]
    raise ValueError(f"Instrument token not found for {exchange}:{symbol}")


def fetch_candles(kite, instrument_token: int, interval: str, lookback_days: int = 5) -> pd.DataFrame:
    """
    Fetch recent historical candles.
    interval: Kite's interval strings, e.g. "5minute", "15minute".
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)

    data = kite.historical_data(instrument_token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]
