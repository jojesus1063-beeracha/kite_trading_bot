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
    """
    df = df.copy()
    bullish = [False] * len(df)
    bearish = [False] * len(df)

    for i in range(1, len(df)):
        prev_row = df.iloc[i - 1]
        curr_row = df.iloc[i]
        bullish[i] = is_bullish_engulfing(prev_row, curr_row)
        bearish[i] = is_bearish_engulfing(prev_row, curr_row)

    df["bullish_engulfing"] = bullish
    df["bearish_engulfing"] = bearish
    return df
