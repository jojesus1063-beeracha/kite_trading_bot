"""
Indicator calculations: EMA and VWAP.

Expects pandas DataFrames with columns: ['date','open','high','low','close','volume']
(this is exactly what Kite Connect's historical_data() returns).
"""

import pandas as pd


def ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Exponential moving average."""
    return df[column].ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Volume-weighted average price, reset each trading day.
    df must include a 'date' column (datetime) plus ohlc+volume.
    """
    df = df.copy()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["_tp_vol"] = typical_price * df["volume"]
    day = df["date"].dt.date
    cum_tp_vol = df.groupby(day)["_tp_vol"].cumsum()
    cum_vol = df.groupby(day)["volume"].cumsum()
    return cum_tp_vol / cum_vol.replace(0, pd.NA)


def average_volume(df: pd.DataFrame, period: int, column: str = "volume") -> pd.Series:
    """Simple rolling average of volume, for entry-candle volume confirmation."""
    return df[column].rolling(window=period).mean()


def add_indicators(df_15m: pd.DataFrame, df_5m: pd.DataFrame, cfg) -> tuple:
    """
    Attach all indicators needed by the strategy to the two timeframe
    DataFrames. Returns (df_15m, df_5m) with new columns added.
    """
    df_15m = df_15m.copy()
    df_15m["ema_fast"] = ema(df_15m, cfg.TREND_EMA_FAST)
    df_15m["ema_slow"] = ema(df_15m, cfg.TREND_EMA_SLOW)
    df_15m["vwap"] = vwap(df_15m)

    df_5m = df_5m.copy()
    df_5m["ema_entry"] = ema(df_5m, cfg.ENTRY_EMA)
    df_5m["avg_volume"] = average_volume(df_5m, cfg.VOLUME_LOOKBACK)

    return df_15m, df_5m
