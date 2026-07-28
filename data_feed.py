"""
Historical candle data via Kite Connect's REST API.

For the live loop, main.py polls historical_data() for the most recent
candles every few minutes rather than aggregating ticks from the
WebSocket — this is simpler and plenty fast enough for a 5-min-entry
strategy. If you later want tick-level precision (e.g. faster stop-loss
reaction), swap this for KiteTicker and aggregate candles yourself.
"""

import time
from datetime import datetime, timedelta

import pandas as pd


def get_instrument_token(kite, symbol: str, exchange: str) -> int:
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst["tradingsymbol"] == symbol:
            return inst["instrument_token"]
    raise ValueError(f"Instrument token not found for {exchange}:{symbol}")


def fetch_candles(kite, instrument_token: int, interval: str, lookback_days: int = 5,
                   max_retries: int = 3) -> pd.DataFrame:
    """
    Fetch recent historical candles.
    interval: Kite's interval strings, e.g. "5minute", "15minute".

    Retries with exponential backoff on transient failures (e.g. Kite's
    rate limiting) instead of letting the exception crash the whole bot.
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)

    last_exc = None
    for attempt in range(max_retries):
        try:
            data = kite.historical_data(instrument_token, from_date, to_date, interval)
            df = pd.DataFrame(data)
            if df.empty:
                return df
            df["date"] = pd.to_datetime(df["date"])
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            last_exc = e
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)

    # All retries exhausted -- return an empty frame so the caller's
    # existing "if df.empty: continue" logic handles it safely instead
    # of crashing the whole process.
    print(f"fetch_candles: giving up after {max_retries} attempts: {last_exc}")
    return pd.DataFrame()
