"""
Pure numpy indicator functions for INTRADAY_OPTIONS_V1 (spec: no pandas,
no pandas-ta, no TA-Lib -- hand-rolled per the dependency decision).

Every function operates on plain Python lists/numpy arrays of completed
candle data, oldest-first. None of these ever look ahead: given N
candles, they only use information available up to and including
candle N-1 (0-indexed) when computing the value "as of" that candle.

Warm-up behavior: every function returns None (or a list with leading
Nones) until enough candles exist to compute a real value. NEVER
silently fills warm-up with 0 -- a caller checking `if value:` would
incorrectly treat a real 0.0 ADX the same as "not ready yet" otherwise,
so we use None explicitly and callers must check `is None`.
"""
import numpy as np
from typing import Optional, List


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """
    Standard EMA, seeded with a SMA of the first `period` values (the
    conventional seeding method). Returns a list the same length as
    `values`; entries before the seed point are None.
    alpha = 2 / (period + 1)
    """
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def true_range(highs: List[float], lows: List[float], closes: List[float]) -> List[Optional[float]]:
    """
    TR[i] = max(high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]|)
    TR[0] is None -- no previous close to compare against yet.
    """
    n = len(highs)
    out: List[Optional[float]] = [None] * n
    for i in range(1, n):
        a = highs[i] - lows[i]
        b = abs(highs[i] - closes[i - 1])
        c = abs(lows[i] - closes[i - 1])
        out[i] = max(a, b, c)
    return out


def atr_wilder(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[Optional[float]]:
    """
    ATR using Wilder smoothing (NOT a simple moving average of TR --
    Wilder's method is its own recursive smoothing, distinct from EMA's
    alpha=2/(n+1)). Seeded with a simple average of the first `period`
    true-range values (index 1..period, since TR[0] is undefined),
    then smoothed: ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period.
    """
    n = len(highs)
    out: List[Optional[float]] = [None] * n
    tr = true_range(highs, lows, closes)
    if n <= period:
        return out
    first_trs = tr[1:period + 1]
    if any(v is None for v in first_trs):
        return out
    seed = sum(first_trs) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        if tr[i] is None:
            continue
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def directional_movement(highs: List[float], lows: List[float]):
    """
    Returns (plus_dm, minus_dm) lists, index 0 always None (no prior
    candle to compare). Standard Wilder DM rule: only the LARGER of
    up-move/down-move counts, and only if positive; the other is 0.
    """
    n = len(highs)
    plus_dm: List[Optional[float]] = [None] * n
    minus_dm: List[Optional[float]] = [None] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
    return plus_dm, minus_dm


def _wilder_smooth(values: List[Optional[float]], period: int, start_index: int) -> List[Optional[float]]:
    """Generic Wilder smoothing helper: seed = sum of first `period`
    non-None values starting at start_index, then recursive smoothing."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    window = values[start_index:start_index + period]
    if len(window) < period or any(v is None for v in window):
        return out
    seed = sum(window)
    seed_idx = start_index + period - 1
    out[seed_idx] = seed
    prev = seed
    for i in range(seed_idx + 1, n):
        if values[i] is None:
            continue
        prev = prev - (prev / period) + values[i]
        out[i] = prev
    return out


def di_plus_minus(highs: List[float], lows: List[float], closes: List[float], period: int):
    """
    Returns (di_plus, di_minus) as percentages, using Wilder-smoothed
    +DM/-DM divided by Wilder-smoothed ATR (in TR units, not the
    ATR/period average -- this matches the standard DI formula:
    DI+ = 100 * smoothed(+DM) / smoothed(TR)).
    """
    n = len(highs)
    plus_dm, minus_dm = directional_movement(highs, lows)
    tr = true_range(highs, lows, closes)

    smoothed_plus_dm = _wilder_smooth(plus_dm, period, start_index=1)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period, start_index=1)
    smoothed_tr = _wilder_smooth(tr, period, start_index=1)

    di_plus: List[Optional[float]] = [None] * n
    di_minus: List[Optional[float]] = [None] * n
    for i in range(n):
        if smoothed_tr[i] is None or smoothed_plus_dm[i] is None or smoothed_minus_dm[i] is None:
            continue
        if smoothed_tr[i] == 0:
            continue
        di_plus[i] = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
        di_minus[i] = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
    return di_plus, di_minus


def adx_wilder(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[Optional[float]]:
    """
    ADX = Wilder-smoothed average of DX, where
    DX[i] = 100 * |DI+ - DI-| / (DI+ + DI-).
    Needs roughly 2*period candles of warm-up before the first real
    value (period for DI+/DI- to stabilize, then another period to
    smooth DX into ADX) -- this is inherent to the indicator, not a
    bug; callers must expect None for a while.
    """
    n = len(highs)
    di_plus, di_minus = di_plus_minus(highs, lows, closes, period)
    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if di_plus[i] is None or di_minus[i] is None:
            continue
        total = di_plus[i] + di_minus[i]
        if total == 0:
            dx[i] = 0.0
            continue
        dx[i] = 100 * abs(di_plus[i] - di_minus[i]) / total

    first_dx_idx = next((i for i, v in enumerate(dx) if v is not None), None)
    out: List[Optional[float]] = [None] * n
    if first_dx_idx is None or n < first_dx_idx + period:
        return out
    window = dx[first_dx_idx:first_dx_idx + period]
    if any(v is None for v in window):
        return out
    seed = sum(window) / period
    seed_idx = first_dx_idx + period - 1
    out[seed_idx] = seed
    prev = seed
    for i in range(seed_idx + 1, n):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def session_vwap(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> List[Optional[float]]:
    """
    Cumulative session VWAP using typical price (H+L+C)/3, resetting
    implicitly at the start of the passed-in array -- CALLER is
    responsible for passing only candles from the start of the trading
    session, never data spanning multiple days, or this silently
    computes a multi-day VWAP instead of a session VWAP.
    """
    n = len(highs)
    out: List[Optional[float]] = [None] * n
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * volumes[i]
        cum_vol += volumes[i]
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


def rolling_avg_volume(volumes: List[float], lookback: int) -> List[Optional[float]]:
    """Simple rolling average of the PRIOR `lookback` candles' volume
    (excludes the current candle -- 'average volume to compare the
    current candle against', not including itself)."""
    n = len(volumes)
    out: List[Optional[float]] = [None] * n
    for i in range(lookback, n):
        window = volumes[i - lookback:i]
        out[i] = sum(window) / lookback
    return out


def volume_ratio(volumes: List[float], lookback: int) -> List[Optional[float]]:
    """current candle's volume / rolling average of the prior `lookback`
    candles. None until the rolling average itself is available."""
    n = len(volumes)
    avg = rolling_avg_volume(volumes, lookback)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        if avg[i] is None or avg[i] == 0:
            continue
        out[i] = volumes[i] / avg[i]
    return out
