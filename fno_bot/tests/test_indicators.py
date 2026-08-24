"""
Tests for indicators.py. Uses small, hand-checkable datasets -- no
runtime dependency on pandas/ta-lib to validate against (per the
dependency decision), so several tests hand-compute the expected
value directly in the assertion rather than comparing to a library.
"""
import math
from fno_bot.strategies.indicators import (
    ema, true_range, atr_wilder, directional_movement, di_plus_minus,
    adx_wilder, session_vwap, rolling_avg_volume, volume_ratio,
)


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_ema_warmup_is_none():
    vals = [1, 2, 3]
    result = ema(vals, period=5)
    assert all(v is None for v in result), "EMA must be None before enough candles exist"


def test_ema_seed_is_sma():
    vals = [10, 20, 30, 40, 50]
    result = ema(vals, period=5)
    assert result[4] == 30.0, "EMA seed must equal the simple average of the first `period` values"


def test_ema_known_value():
    vals = [1, 2, 3, 4, 5]
    result = ema(vals, period=3)
    seed = (1 + 2 + 3) / 3
    assert approx(result[2], seed)
    expected_3 = 4 * 0.5 + seed * 0.5
    assert approx(result[3], expected_3), f"expected {expected_3}, got {result[3]}"
    expected_4 = 5 * 0.5 + expected_3 * 0.5
    assert approx(result[4], expected_4), f"expected {expected_4}, got {result[4]}"


def test_true_range_first_is_none():
    tr = true_range([10, 12], [8, 9], [9, 11])
    assert tr[0] is None


def test_true_range_known_value():
    tr = true_range([10, 15], [9, 11], [10, 13])
    assert tr[1] == 5.0, f"expected 5.0, got {tr[1]}"


def test_atr_wilder_warmup():
    highs = [10, 11, 12]
    lows = [9, 10, 11]
    closes = [9.5, 10.5, 11.5]
    result = atr_wilder(highs, lows, closes, period=5)
    assert all(v is None for v in result), "ATR must be None with insufficient candles"


def test_atr_wilder_seed_matches_manual_average():
    highs = [10, 12, 14, 16, 18, 20]
    lows = [8, 10, 12, 14, 16, 18]
    closes = [9, 11, 13, 15, 17, 19]
    result = atr_wilder(highs, lows, closes, period=5)
    assert result[5] is not None
    assert approx(result[5], 3.0), f"expected 3.0, got {result[5]}"


def test_directional_movement_pure_uptrend():
    highs = [10, 12, 14, 16]
    lows = [8, 10, 12, 14]
    plus_dm, minus_dm = directional_movement(highs, lows)
    assert plus_dm[0] is None
    for i in range(1, 4):
        assert approx(plus_dm[i], 2.0), f"index {i}: expected plus_dm=2.0, got {plus_dm[i]}"
        assert approx(minus_dm[i], 0.0), f"index {i}: expected minus_dm=0.0, got {minus_dm[i]}"


def test_adx_none_during_warmup():
    n = 10
    highs = [10 + i for i in range(n)]
    lows = [8 + i for i in range(n)]
    closes = [9 + i for i in range(n)]
    result = adx_wilder(highs, lows, closes, period=14)
    assert all(v is None for v in result), "ADX needs ~2x period candles; 10 candles at period=14 must all be None"


def test_adx_strong_trend_is_high():
    n = 40
    highs = [100 + i * 2 for i in range(n)]
    lows = [98 + i * 2 for i in range(n)]
    closes = [99 + i * 2 for i in range(n)]
    result = adx_wilder(highs, lows, closes, period=14)
    last_valid = [v for v in result if v is not None]
    assert len(last_valid) > 0, "expected ADX to become available within 40 candles at period=14"
    assert last_valid[-1] > 50, f"expected strong sustained trend to produce high ADX, got {last_valid[-1]}"


def test_session_vwap_known_value():
    result = session_vwap([10], [8], [9], [100])
    assert approx(result[0], 9.0)
    result2 = session_vwap([10, 12], [8, 10], [9, 11], [100, 200])
    expected = (9 * 100 + 11 * 200) / 300
    assert approx(result2[1], expected), f"expected {expected}, got {result2[1]}"


def test_rolling_avg_volume():
    vols = [10, 20, 30, 40, 50]
    result = rolling_avg_volume(vols, lookback=3)
    assert result[0] is None and result[1] is None and result[2] is None
    assert approx(result[3], 20.0), f"expected 20.0, got {result[3]}"
    assert approx(result[4], 30.0), f"expected 30.0, got {result[4]}"


def test_volume_ratio():
    vols = [10, 20, 30, 40, 50]
    result = volume_ratio(vols, lookback=3)
    assert result[3] is not None
    assert approx(result[3], 2.0), f"expected 2.0, got {result[3]}"


def test_no_future_leakage_ema():
    vals_short = [1, 2, 3, 4, 5]
    vals_long = [1, 2, 3, 4, 5, 999, -999, 12345]
    result_short = ema(vals_short, period=3)
    result_long = ema(vals_long, period=3)
    for i in range(len(vals_short)):
        a, b = result_short[i], result_long[i]
        if a is None:
            assert b is None
        else:
            assert approx(a, b), f"index {i}: value changed when future candles were appended ({a} vs {b}) -- FUTURE LEAKAGE"


def test_no_future_leakage_adx():
    n = 30
    highs = [100 + i for i in range(n)]
    lows = [98 + i for i in range(n)]
    closes = [99 + i for i in range(n)]
    future_highs = highs + [500, 1, 900]
    future_lows = lows + [1, 500, 1]
    future_closes = closes + [200, 200, 200]

    result_short = adx_wilder(highs, lows, closes, period=14)
    result_long = adx_wilder(future_highs, future_lows, future_closes, period=14)
    for i in range(n):
        a, b = result_short[i], result_long[i]
        if a is None:
            assert b is None
        else:
            assert approx(a, b), f"index {i}: ADX changed when future candles appended -- FUTURE LEAKAGE"
