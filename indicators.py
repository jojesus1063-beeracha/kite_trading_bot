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


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index (Wilder's original method) — measures
    trend STRENGTH (0-100), regardless of direction. Used as a filter:
    a high ADX means "there's a real trend happening right now", a low
    ADX means the market is choppy/sideways even if price is above or
    below an EMA.

    This does NOT tell you the direction — pair it with the existing
    EMA/VWAP trend check for that.
    """
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    # Wilder's smoothing (an EMA-like recursive average with alpha = 1/period)
    alpha = 1.0 / period
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    plus_di = 100 * (smoothed_plus_dm / smoothed_tr)
    minus_di = 100 * (smoothed_minus_dm / smoothed_tr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def add_indicators(df_15m: pd.DataFrame, df_5m: pd.DataFrame, cfg) -> tuple:
    """
    Attach all indicators needed by the strategy to the two timeframe
    DataFrames. Returns (df_15m, df_5m) with new columns added.
    """
    df_15m = df_15m.copy()
    df_15m["ema_fast"] = ema(df_15m, cfg.TREND_EMA_FAST)
    df_15m["ema_slow"] = ema(df_15m, cfg.TREND_EMA_SLOW)
    df_15m["vwap"] = vwap(df_15m)
    df_15m["adx"] = adx(df_15m, getattr(cfg, "ADX_PERIOD", 14))

    df_5m = df_5m.copy()
    df_5m["ema_entry"] = ema(df_5m, cfg.ENTRY_EMA)
    df_5m["avg_volume"] = average_volume(df_5m, cfg.VOLUME_LOOKBACK)

    return df_15m, df_5m
