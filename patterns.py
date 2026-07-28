"""
Candle pattern detection — bullish/bearish engulfing.

A bullish engulfing candle:
  - previous candle is bearish (close < open)
  - current candle is bullish (close > open)
  - current candle's body fully engulfs the previous candle's body
    (current open <= previous close, current close >= previous open)

Bearish engulfing is the mirror image.
"""

import pandas as pd


def is_bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    engulfs = (curr["open"] <= prev["close"]) and (curr["close"] >= prev["open"])
    return bool(prev_bearish and curr_bullish and engulfs)


def is_bearish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    prev_bullish = prev["close"] > prev["open"]
    curr_bearish = curr["close"] < curr["open"]
    engulfs = (curr["open"] >= prev["close"]) and (curr["close"] <= prev["open"])
    return bool(prev_bullish and curr_bearish and engulfs)


def label_engulfing_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds two boolean columns to a 5-min OHLCV DataFrame:
    'bullish_engulfing' and 'bearish_engulfing', evaluated at each row
    using that row and the one before it.

    Vectorized (no Python-level row loop) -- produces identical results
    to the row-by-row version, but is O(n) instead of O(n) per call,
    which matters a lot when this gets called repeatedly on a growing
    slice (e.g. backtest.py's bar-by-bar walk-forward loop).
    """
    df = df.copy()

    prev_open = df["open"].shift(1)
    prev_close = df["close"].shift(1)

    prev_bearish = prev_close < prev_open
    prev_bullish = prev_close > prev_open
    curr_bullish = df["close"] > df["open"]
    curr_bearish = df["close"] < df["open"]

    engulfs_up = (df["open"] <= prev_close) & (df["close"] >= prev_open)
    engulfs_down = (df["open"] >= prev_close) & (df["close"] <= prev_open)

    bullish_engulfing = (prev_bearish & curr_bullish & engulfs_up).fillna(False)
    bearish_engulfing = (prev_bullish & curr_bearish & engulfs_down).fillna(False)

    # Row 0 has no previous candle -- match the original's implicit False there.
    bullish_engulfing.iloc[0] = False
    bearish_engulfing.iloc[0] = False

    df["bullish_engulfing"] = bullish_engulfing
    df["bearish_engulfing"] = bearish_engulfing
    return df
