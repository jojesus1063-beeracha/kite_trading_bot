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

from scheduler import candle_interval_minutes


def get_instrument_token(kite, symbol: str, exchange: str) -> int:
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst["tradingsymbol"] == symbol:
            return inst["instrument_token"]
    raise ValueError(f"Instrument token not found for {exchange}:{symbol}")


def trim_incomplete_candles(df, interval_minutes, buffer_seconds=10, now=None):
    """
    Removes any trailing candle(s) that have NOT fully finished forming
    yet. Kite returns the currently-forming candle as a live-updating
    row in historical_data() -- e.g. a 15-min candle that started 4
    minutes ago already shows real (but unstable, still-changing)
    OHLC. Using such a row as "the latest completed candle" means
    trend/signal decisions are made against noisy, evolving data
    rather than a genuinely closed bar.

    A candle starting at Wed Jul 29 10:33:59 IST 2026 covers [date, date + interval_minutes).
    It is only eligible once that full interval has elapsed, plus a
    small safety buffer (default 10s) to protect against querying
    right at the boundary before the broker has finalized the candle.
    """
    if df is None or df.empty:
        return df
    if now is None:
        now = pd.Timestamp.now(tz=df["date"].dt.tz)
    candle_end = df["date"] + pd.Timedelta(minutes=interval_minutes)
    eligible_at = candle_end + pd.Timedelta(seconds=buffer_seconds)
    return df[eligible_at <= now].reset_index(drop=True)


def fetch_candles(kite, instrument_token: int, interval: str, lookback_days: int = 5,
                   max_retries: int = 3, trim_incomplete: bool = True) -> pd.DataFrame:
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
            df = df[["date", "open", "high", "low", "close", "volume"]]
            if trim_incomplete:
                df = trim_incomplete_candles(df, candle_interval_minutes(interval))
            return df
        except Exception as e:
            last_exc = e
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)

    # All retries exhausted -- return an empty frame so the caller's
    # existing "if df.empty: continue" logic handles it safely instead
    # of crashing the whole process.
    print(f"fetch_candles: giving up after {max_retries} attempts: {last_exc}")
    return pd.DataFrame()
