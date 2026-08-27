"""Indicator calculations: EMA, VWAP, ATR, ADX and Wilder directional indicators."""
import pandas as pd


def ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["_tp_vol"] = typical_price * df["volume"]
    day = df["date"].dt.date
    cum_tp_vol = df.groupby(day)["_tp_vol"].cumsum()
    cum_vol = df.groupby(day)["volume"].cumsum()
    return cum_tp_vol / cum_vol.replace(0, pd.NA)


def average_volume(df: pd.DataFrame, period: int, column: str = "volume") -> pd.Series:
    return df[column].rolling(window=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def directional_indicators(df: pd.DataFrame, period: int = 14):
    """Return (+DI, -DI, ADX) using the exact Wilder smoothing formerly inside adx()."""
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]
    prev_close, prev_high, prev_low = close.shift(1), high.shift(1), low.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    up_move, down_move = high-prev_high, prev_low-low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_mask = (up_move > down_move) & (up_move > 0)
    minus_mask = (down_move > up_move) & (down_move > 0)
    plus_dm[plus_mask] = up_move[plus_mask]
    minus_dm[minus_mask] = down_move[minus_mask]
    alpha = 1.0 / period
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * (smoothed_plus_dm / smoothed_tr)
    minus_di = 100 * (smoothed_minus_dm / smoothed_tr)
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di)
    adx_series = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return plus_di, minus_di, adx_series


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return directional_indicators(df, period)[2]


def add_indicators(df_15m: pd.DataFrame, df_5m: pd.DataFrame, cfg) -> tuple:
    df_15m = df_15m.copy()
    df_15m["ema_fast"] = ema(df_15m, cfg.TREND_EMA_FAST)
    df_15m["ema_slow"] = ema(df_15m, cfg.TREND_EMA_SLOW)
    df_15m["vwap"] = vwap(df_15m)
    pdi, mdi, adx_series = directional_indicators(df_15m, getattr(cfg, "ADX_PERIOD", 14))
    df_15m["plus_di"] = pdi
    df_15m["minus_di"] = mdi
    df_15m["adx"] = adx_series
    df_5m = df_5m.copy()
    df_5m["ema_entry"] = ema(df_5m, cfg.ENTRY_EMA)
    df_5m["avg_volume"] = average_volume(df_5m, cfg.VOLUME_LOOKBACK)
    return df_15m, df_5m
