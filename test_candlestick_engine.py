"""Pytest regression matrix for the isolated candlestick entry-timing engine.

This file intentionally tests the pure decision module only. It does not touch
broker execution, live trading, CP9, or the aggregate-risk guard.
"""

import pandas as pd
import pytest

from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Pattern,
    Side,
    Setup,
    Trigger,
    add_indicators,
    build_trade_plan,
    confirm_pending,
    detect_setups,
    evaluate_trade_entry,
    inside_squeeze_indices,
    is_bearish_engulfing,
    is_bearish_harami,
    is_bearish_kicker,
    is_bearish_marubozu,
    is_dark_cloud_cover,
    is_doji,
    is_dragonfly_doji,
    is_evening_star,
    is_falling_three_methods,
    is_gravestone_doji,
    is_hammer,
    is_hanging_man,
    is_inverted_hammer,
    is_morning_star,
    is_piercing_line,
    is_rising_three_methods,
    is_shooting_star,
    is_three_black_crows,
    is_three_inside_down,
    is_three_inside_up,
    is_three_white_soldiers,
    is_tweezer_bottom,
    is_tweezer_top,
    is_bullish_engulfing,
    is_bullish_harami,
    is_bullish_kicker,
    is_bullish_marubozu,
    position_size,
)


TICK = 0.05
EQUITY = 5000.0
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)


def candle(open_, high, low, close, volume=2000):
    """Exact OHLCV fixture used by every test."""
    return {
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


def series(open_, high, low, close, volume=2000):
    return pd.Series(candle(open_, high, low, close, volume))


def make_df(pattern_rows, baseline=100.0, warmup=30):
    """Create enough prior bars for SMA20, avg-body, EMA50 and session VWAP."""
    rows = []
    start = pd.Timestamp("2026-08-12 09:15")
    for i in range(warmup):
        close = baseline + (0.02 if i % 2 == 0 else -0.02)
        rows.append({
            "date": start + pd.Timedelta(minutes=3 * i),
            "open": baseline,
            "high": max(baseline, close) + 0.03,
            "low": min(baseline, close) - 0.03,
            "close": close,
            "volume": 1000.0,
        })
    for offset, raw in enumerate(pattern_rows):
        row = dict(raw)
        row["date"] = start + pd.Timedelta(minutes=3 * (warmup + offset))
        rows.append(row)
    return pd.DataFrame(rows)


def enriched(pattern_rows, baseline=100.0):
    return add_indicators(make_df(pattern_rows, baseline), CFG)


def detect(pattern_rows, side, baseline=100.0):
    df = enriched(pattern_rows, baseline)
    return detect_setups(df, len(df) - 1, TICK, CFG, intended_side=side)


def get_setup(setups, pattern):
    matches = [s for s in setups if s.pattern == pattern]
    assert matches, f"{pattern.value} not found; detected={[s.pattern.value for s in setups]}"
    return matches[0]


def append_closed_bar(df, raw):
    row = dict(raw)
    row["date"] = pd.Timestamp(df.iloc[-1]["date"]) + pd.Timedelta(minutes=3)
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


# ---------------------------------------------------------------------------
# Hammer / Hanging Man geometry and routing
# ---------------------------------------------------------------------------

def test_red_hammer_six_x_lower_wick_positive():
    assert is_hammer(series(100.4, 100.5, 99.7, 100.3), CFG)


def test_green_hammer_positive():
    assert is_hammer(series(100.3, 100.5, 99.7, 100.4), CFG)


def test_hammer_negative_when_lower_wick_under_two_x_body():
    assert not is_hammer(series(100.20, 100.45, 99.90, 100.40), CFG)


def test_lower_wick_geometry_routes_hammer_for_buy():
    setups = detect([candle(100.30, 100.50, 99.40, 100.45)], Side.BUY)
    setup = get_setup(setups, Pattern.HAMMER)
    assert setup.side == Side.BUY
    assert setup.trigger == Trigger.BREAKOUT


def test_same_lower_wick_geometry_routes_hanging_man_for_sell():
    setups = detect([candle(100.30, 100.50, 99.40, 100.45)], Side.SELL)
    setup = get_setup(setups, Pattern.HANGING_MAN)
    assert setup.side == Side.SELL
    assert is_hanging_man(enriched([candle(100.30, 100.50, 99.40, 100.45)]).iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Inverted Hammer / Shooting Star disambiguation
# ---------------------------------------------------------------------------

def test_upper_wick_geometry_routes_inverted_hammer_for_buy():
    setups = detect([candle(99.70, 100.75, 99.65, 99.85)], Side.BUY)
    setup = get_setup(setups, Pattern.INVERTED_HAMMER)
    assert setup.side == Side.BUY
    assert is_inverted_hammer(enriched([candle(99.70, 100.75, 99.65, 99.85)]).iloc[-1], CFG)


def test_same_upper_wick_geometry_routes_shooting_star_for_sell():
    setups = detect([candle(99.70, 100.75, 99.65, 99.85)], Side.SELL)
    setup = get_setup(setups, Pattern.SHOOTING_STAR)
    assert setup.side == Side.SELL
    assert is_shooting_star(enriched([candle(99.70, 100.75, 99.65, 99.85)]).iloc[-1], CFG)


def test_upper_rejection_negative_when_upper_wick_under_two_x_body():
    c = series(99.70, 100.10, 99.65, 99.90)
    assert not is_inverted_hammer(c, CFG)
    assert not is_shooting_star(c, CFG)


# ---------------------------------------------------------------------------
# Marubozu + volume negatives
# ---------------------------------------------------------------------------

def test_bullish_marubozu_positive_and_routed_buy():
    df = enriched([candle(100.00, 102.02, 99.98, 102.00)])
    assert is_bullish_marubozu(df.iloc[-1], CFG)
    setup = get_setup(detect_setups(df, len(df) - 1, TICK, CFG, intended_side=Side.BUY), Pattern.BULLISH_MARUBOZU)
    assert setup.side == Side.BUY


def test_bullish_marubozu_low_volume_negative():
    setups = detect([candle(100.00, 102.02, 99.98, 102.00, volume=500)], Side.BUY)
    assert all(s.pattern != Pattern.BULLISH_MARUBOZU for s in setups)


def test_bearish_marubozu_positive_and_routed_sell():
    df = enriched([candle(100.00, 100.02, 97.98, 98.00)])
    assert is_bearish_marubozu(df.iloc[-1], CFG)
    setup = get_setup(detect_setups(df, len(df) - 1, TICK, CFG, intended_side=Side.SELL), Pattern.BEARISH_MARUBOZU)
    assert setup.side == Side.SELL


def test_bearish_marubozu_low_volume_negative():
    setups = detect([candle(100.00, 100.02, 97.98, 98.00, volume=500)], Side.SELL)
    assert all(s.pattern != Pattern.BEARISH_MARUBOZU for s in setups)


# ---------------------------------------------------------------------------
# Doji family + closed-bar confirmation
# ---------------------------------------------------------------------------

def test_standard_doji_positive():
    assert is_doji(series(100.00, 101.00, 99.00, 100.05), CFG)


def test_dragonfly_doji_positive():
    df = enriched([candle(100.40, 100.45, 99.40, 100.42)])
    assert is_dragonfly_doji(df.iloc[-1], CFG)


def test_dragonfly_doji_confirms_only_on_next_closed_bar():
    pattern = candle(100.40, 100.45, 99.40, 100.42)
    df1 = enriched([pattern])
    setup = get_setup(detect_setups(df1, len(df1) - 1, TICK, CFG, intended_side=Side.BUY), Pattern.DRAGONFLY_DOJI)
    df2 = enriched([pattern, candle(100.50, 101.40, 100.40, 101.20, volume=2500)])
    plan = confirm_pending(df2, setup, len(df2) - 1, EQUITY, CFG)
    assert plan is not None
    assert plan.pattern == Pattern.DRAGONFLY_DOJI
    assert plan.side == Side.BUY


def test_dragonfly_intrabar_touch_without_close_is_negative():
    pattern = candle(100.40, 100.45, 99.40, 100.42)
    df1 = enriched([pattern])
    setup = get_setup(detect_setups(df1, len(df1) - 1, TICK, CFG, intended_side=Side.BUY), Pattern.DRAGONFLY_DOJI)
    df2 = enriched([pattern, candle(100.40, 100.80, 100.30, 100.45, volume=2500)])
    assert confirm_pending(df2, setup, len(df2) - 1, EQUITY, CFG) is None


def test_gravestone_doji_positive():
    df = enriched([candle(99.60, 100.60, 99.55, 99.58)])
    assert is_gravestone_doji(df.iloc[-1], CFG)


def test_gravestone_doji_confirms_only_on_next_closed_bar():
    pattern = candle(99.60, 100.60, 99.55, 99.58)
    df1 = enriched([pattern])
    setup = get_setup(detect_setups(df1, len(df1) - 1, TICK, CFG, intended_side=Side.SELL), Pattern.GRAVESTONE_DOJI)
    df2 = enriched([pattern, candle(99.50, 99.60, 98.70, 98.80, volume=2500)])
    plan = confirm_pending(df2, setup, len(df2) - 1, EQUITY, CFG)
    assert plan is not None
    assert plan.pattern == Pattern.GRAVESTONE_DOJI
    assert plan.side == Side.SELL


def test_gravestone_intrabar_touch_without_close_is_negative():
    pattern = candle(99.60, 100.60, 99.55, 99.58)
    df1 = enriched([pattern])
    setup = get_setup(detect_setups(df1, len(df1) - 1, TICK, CFG, intended_side=Side.SELL), Pattern.GRAVESTONE_DOJI)
    df2 = enriched([pattern, candle(99.60, 99.80, 99.30, 99.55, volume=2500)])
    assert confirm_pending(df2, setup, len(df2) - 1, EQUITY, CFG) is None


# ---------------------------------------------------------------------------
# Engulfing + NEXT_OPEN no-lookahead state machine
# ---------------------------------------------------------------------------

def test_bullish_engulfing_positive():
    assert is_bullish_engulfing(series(100.5, 100.6, 99.8, 100.0), series(99.9, 100.8, 99.7, 100.7))


def test_bullish_engulfing_geometry_negative():
    assert not is_bullish_engulfing(series(100.5, 100.6, 99.8, 100.0), series(100.1, 100.7, 99.9, 100.6))


def test_bearish_engulfing_positive():
    assert is_bearish_engulfing(series(100.0, 100.6, 99.9, 100.5), series(100.6, 100.7, 99.7, 99.8))


def test_bearish_engulfing_geometry_negative():
    assert not is_bearish_engulfing(series(100.0, 100.6, 99.9, 100.5), series(100.4, 100.5, 99.8, 99.9))


def test_next_open_trigger_enters_waiting_without_future_bar_access():
    rows = [
        candle(100.5, 100.6, 99.8, 100.0),
        candle(99.9, 100.8, 99.7, 100.7),
    ]
    engine = CandlestickEngine(CFG)
    result = evaluate_trade_entry("NEXTOPEN", make_df(rows), "BUY", EQUITY, TICK, engine)
    assert result.state == GateState.WAITING
    assert result.setup is not None
    assert result.setup.pattern == Pattern.BULLISH_ENGULFING
    assert result.setup.trigger == Trigger.NEXT_OPEN
    assert result.plan is None


def test_next_open_confirms_only_after_next_completed_bar_is_supplied():
    rows = [
        candle(100.5, 100.6, 99.8, 100.0),
        candle(99.9, 100.8, 99.7, 100.7),
    ]
    engine = CandlestickEngine(CFG)
    first = evaluate_trade_entry("NEXTOPEN2", make_df(rows), "BUY", EQUITY, TICK, engine)
    assert first.state == GateState.WAITING
    rows.append(candle(100.8, 101.5, 100.7, 101.3, volume=2500))
    second = evaluate_trade_entry("NEXTOPEN2", make_df(rows), "BUY", EQUITY, TICK, engine)
    assert second.state == GateState.CONFIRMED
    assert second.plan is not None
    assert second.plan.pattern == Pattern.BULLISH_ENGULFING
    assert second.plan.entry_price == pytest.approx(100.8)


# ---------------------------------------------------------------------------
# Piercing Line / Dark Cloud Cover
# ---------------------------------------------------------------------------

def test_piercing_line_positive_above_fifty_percent():
    assert is_piercing_line(series(101.0, 101.2, 98.8, 99.0), series(98.6, 100.5, 98.5, 100.2))


def test_piercing_line_negative_at_fifty_percent():
    assert not is_piercing_line(series(101.0, 101.2, 98.8, 99.0), series(98.6, 100.1, 98.5, 100.0))


def test_dark_cloud_cover_positive_below_fifty_percent():
    assert is_dark_cloud_cover(series(99.0, 101.2, 98.8, 101.0), series(101.4, 101.5, 99.5, 99.8))


def test_dark_cloud_cover_negative_at_fifty_percent():
    assert not is_dark_cloud_cover(series(99.0, 101.2, 98.8, 101.0), series(101.4, 101.5, 99.9, 100.0))


# ---------------------------------------------------------------------------
# Harami
# ---------------------------------------------------------------------------

def test_bullish_harami_positive_inside_body():
    df = enriched([candle(101.0, 101.1, 98.9, 99.0), candle(99.4, 100.3, 99.3, 100.2)])
    assert is_bullish_harami(df.iloc[-2], df.iloc[-1], CFG)


def test_bullish_harami_negative_when_second_body_escapes():
    df = enriched([candle(101.0, 101.1, 98.9, 99.0), candle(99.4, 101.3, 99.3, 101.2)])
    assert not is_bullish_harami(df.iloc[-2], df.iloc[-1], CFG)


def test_bearish_harami_positive_inside_body():
    df = enriched([candle(99.0, 101.1, 98.9, 101.0), candle(100.6, 100.7, 99.7, 99.8)])
    assert is_bearish_harami(df.iloc[-2], df.iloc[-1], CFG)


def test_bearish_harami_negative_when_second_body_escapes():
    df = enriched([candle(99.0, 101.1, 98.9, 101.0), candle(100.6, 100.7, 98.7, 98.8)])
    assert not is_bearish_harami(df.iloc[-2], df.iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Tweezers
# ---------------------------------------------------------------------------

def test_tweezer_bottom_positive_within_tolerance():
    assert is_tweezer_bottom(series(100.5, 100.6, 99.9, 100.0), series(100.0, 100.5, 99.92, 100.4), CFG)


def test_tweezer_bottom_negative_outside_tolerance():
    assert not is_tweezer_bottom(series(100.5, 100.6, 99.9, 100.0), series(100.0, 100.5, 99.7, 100.4), CFG)


def test_tweezer_top_positive_within_tolerance():
    assert is_tweezer_top(series(100.0, 100.5, 99.8, 100.4), series(100.4, 100.52, 99.9, 100.0), CFG)


def test_tweezer_top_negative_outside_tolerance():
    assert not is_tweezer_top(series(100.0, 100.5, 99.8, 100.4), series(100.4, 100.8, 99.9, 100.0), CFG)


# ---------------------------------------------------------------------------
# Kickers
# ---------------------------------------------------------------------------

def test_bullish_kicker_positive_gap_and_reversal():
    df = enriched([candle(100.5, 100.6, 98.9, 99.0), candle(101.0, 102.6, 100.8, 102.5)])
    assert is_bullish_kicker(df.iloc[-2], df.iloc[-1], CFG)


def test_bullish_kicker_negative_without_gap():
    df = enriched([candle(100.5, 100.6, 98.9, 99.0), candle(100.3, 102.0, 100.2, 101.9)])
    assert not is_bullish_kicker(df.iloc[-2], df.iloc[-1], CFG)


def test_bearish_kicker_positive_gap_and_reversal():
    df = enriched([candle(99.5, 101.1, 99.4, 101.0), candle(99.0, 99.2, 97.4, 97.5)])
    assert is_bearish_kicker(df.iloc[-2], df.iloc[-1], CFG)


def test_bearish_kicker_negative_without_gap():
    df = enriched([candle(99.5, 101.1, 99.4, 101.0), candle(99.2, 99.5, 97.6, 97.7)])
    assert not is_bearish_kicker(df.iloc[-2], df.iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Morning / Evening Star
# ---------------------------------------------------------------------------

def test_morning_star_positive():
    df = enriched([
        candle(101.0, 101.1, 98.9, 99.0),
        candle(98.8, 99.0, 98.6, 98.9),
        candle(99.0, 100.8, 98.9, 100.6),
    ])
    assert is_morning_star(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_morning_star_negative_without_fifty_percent_recovery():
    df = enriched([
        candle(101.0, 101.1, 98.9, 99.0),
        candle(98.8, 99.0, 98.6, 98.9),
        candle(99.0, 100.0, 98.9, 99.9),
    ])
    assert not is_morning_star(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_evening_star_positive():
    df = enriched([
        candle(99.0, 101.1, 98.9, 101.0),
        candle(101.1, 101.3, 101.0, 101.2),
        candle(101.0, 101.1, 99.2, 99.4),
    ])
    assert is_evening_star(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_evening_star_negative_without_fifty_percent_reversal():
    df = enriched([
        candle(99.0, 101.1, 98.9, 101.0),
        candle(101.1, 101.3, 101.0, 101.2),
        candle(101.0, 101.1, 99.9, 100.1),
    ])
    assert not is_evening_star(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Three White Soldiers / Three Black Crows
# ---------------------------------------------------------------------------

def test_three_white_soldiers_positive():
    df = enriched([
        candle(100.0, 100.9, 99.95, 100.8),
        candle(100.4, 101.3, 100.35, 101.2),
        candle(100.8, 101.7, 100.75, 101.6),
    ])
    assert is_three_white_soldiers(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_white_soldiers_negative_when_third_does_not_advance():
    df = enriched([
        candle(100.0, 100.9, 99.95, 100.8),
        candle(100.4, 101.3, 100.35, 101.2),
        candle(100.8, 101.1, 100.75, 101.0),
    ])
    assert not is_three_white_soldiers(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_black_crows_positive():
    df = enriched([
        candle(100.8, 100.85, 99.9, 100.0),
        candle(100.4, 100.45, 99.5, 99.6),
        candle(100.0, 100.05, 99.1, 99.2),
    ])
    assert is_three_black_crows(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_black_crows_negative_when_third_does_not_decline():
    df = enriched([
        candle(100.8, 100.85, 99.9, 100.0),
        candle(100.4, 100.45, 99.5, 99.6),
        candle(100.0, 100.05, 99.6, 99.8),
    ])
    assert not is_three_black_crows(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Three Inside Up / Down
# ---------------------------------------------------------------------------

def test_three_inside_up_positive():
    df = enriched([
        candle(101.0, 101.1, 98.9, 99.0),
        candle(99.4, 100.1, 99.3, 100.0),
        candle(100.0, 101.4, 99.9, 101.3),
    ])
    assert is_three_inside_up(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_inside_up_negative_without_break_above_first_high():
    df = enriched([
        candle(101.0, 101.1, 98.9, 99.0),
        candle(99.4, 100.1, 99.3, 100.0),
        candle(100.0, 101.05, 99.9, 101.0),
    ])
    assert not is_three_inside_up(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_inside_down_positive():
    df = enriched([
        candle(99.0, 101.1, 98.9, 101.0),
        candle(100.6, 100.7, 99.7, 99.8),
        candle(99.8, 99.9, 98.6, 98.7),
    ])
    assert is_three_inside_down(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


def test_three_inside_down_negative_without_break_below_first_low():
    df = enriched([
        candle(99.0, 101.1, 98.9, 101.0),
        candle(100.6, 100.7, 99.7, 99.8),
        candle(99.8, 99.9, 98.95, 99.0),
    ])
    assert not is_three_inside_down(df.iloc[-3], df.iloc[-2], df.iloc[-1], CFG)


# ---------------------------------------------------------------------------
# Rising / Falling Three Methods
# ---------------------------------------------------------------------------

def test_rising_three_methods_positive():
    df = enriched([
        candle(100.0, 102.2, 99.8, 102.0),
        candle(101.8, 101.9, 101.3, 101.4),
        candle(101.5, 101.6, 101.0, 101.1),
        candle(101.2, 101.3, 100.7, 100.8),
        candle(101.0, 102.5, 100.9, 102.4),
    ])
    bars = [df.iloc[j] for j in range(len(df) - 5, len(df))]
    assert is_rising_three_methods(bars, CFG)


def test_rising_three_methods_negative_if_middle_breaks_first_range():
    df = enriched([
        candle(100.0, 102.2, 99.8, 102.0),
        candle(101.8, 101.9, 101.3, 101.4),
        candle(101.5, 102.3, 101.0, 101.1),
        candle(101.2, 101.3, 100.7, 100.8),
        candle(101.0, 102.5, 100.9, 102.4),
    ])
    bars = [df.iloc[j] for j in range(len(df) - 5, len(df))]
    assert not is_rising_three_methods(bars, CFG)


def test_falling_three_methods_positive():
    df = enriched([
        candle(102.0, 102.2, 99.8, 100.0),
        candle(100.2, 100.7, 100.1, 100.6),
        candle(100.5, 101.0, 100.4, 100.9),
        candle(100.8, 101.3, 100.7, 101.2),
        candle(101.0, 101.1, 99.5, 99.6),
    ])
    bars = [df.iloc[j] for j in range(len(df) - 5, len(df))]
    assert is_falling_three_methods(bars, CFG)


def test_falling_three_methods_negative_if_middle_breaks_first_range():
    df = enriched([
        candle(102.0, 102.2, 99.8, 100.0),
        candle(100.2, 100.7, 100.1, 100.6),
        candle(100.5, 101.0, 99.7, 100.9),
        candle(100.8, 101.3, 100.7, 101.2),
        candle(101.0, 101.1, 99.5, 99.6),
    ])
    bars = [df.iloc[j] for j in range(len(df) - 5, len(df))]
    assert not is_falling_three_methods(bars, CFG)


# ---------------------------------------------------------------------------
# Inside Bar Squeeze
# ---------------------------------------------------------------------------

def test_inside_bar_squeeze_positive_and_breakout_confirmation():
    mother = candle(100.0, 101.0, 99.0, 100.2)
    inside1 = candle(100.2, 100.7, 99.4, 100.3)
    inside2 = candle(100.3, 100.8, 99.3, 100.55)
    engine = CandlestickEngine(CFG)
    waiting = evaluate_trade_entry("SQUEEZE", make_df([mother, inside1, inside2]), "BUY", EQUITY, TICK, engine)
    assert waiting.state == GateState.WAITING
    assert waiting.setup is not None
    assert waiting.setup.pattern == Pattern.INSIDE_BAR_SQUEEZE
    assert waiting.setup.trigger == Trigger.BREAKOUT
    confirmed = evaluate_trade_entry(
        "SQUEEZE",
        make_df([mother, inside1, inside2, candle(100.8, 101.4, 100.7, 101.2, volume=2500)]),
        "BUY",
        EQUITY,
        TICK,
        engine,
    )
    assert confirmed.state == GateState.CONFIRMED
    assert confirmed.plan is not None
    assert confirmed.plan.pattern == Pattern.INSIDE_BAR_SQUEEZE


def test_inside_bar_squeeze_negative_when_child_breaks_mother_range():
    df = enriched([
        candle(100.0, 101.0, 99.0, 100.2),
        candle(100.2, 100.7, 99.4, 100.3),
        candle(100.3, 101.1, 99.3, 100.4),
    ])
    assert inside_squeeze_indices(df, len(df) - 1) is None


# ---------------------------------------------------------------------------
# Pending expiration: lifetime is exactly two completed bars
# ---------------------------------------------------------------------------

def test_pending_setup_survives_bar_one_and_bar_two_then_expires_on_bar_three():
    base = make_df([candle(100.30, 100.50, 99.40, 100.45)])
    setup_index = len(base) - 1
    pending = Setup(
        Pattern.HAMMER,
        Side.BUY,
        Trigger.BREAKOUT,
        setup_index,
        (setup_index,),
        100.55,
        99.35,
        "expiration regression",
    )
    engine = CandlestickEngine(CFG)
    engine.pending["EXPIRE"] = [pending]

    frame1 = append_closed_bar(base, candle(100.40, 100.50, 100.20, 100.45, volume=500))
    r1 = evaluate_trade_entry("EXPIRE", frame1, "BUY", EQUITY, TICK, engine)
    assert r1.state == GateState.WAITING

    frame2 = append_closed_bar(frame1, candle(100.40, 100.50, 100.20, 100.44, volume=500))
    r2 = evaluate_trade_entry("EXPIRE", frame2, "BUY", EQUITY, TICK, engine)
    assert r2.state == GateState.WAITING

    frame3 = append_closed_bar(frame2, candle(100.40, 100.50, 100.20, 100.43, volume=500))
    r3 = evaluate_trade_entry("EXPIRE", frame3, "BUY", EQUITY, TICK, engine)
    assert r3.state == GateState.NO_PATTERN
    assert engine.pending.get("EXPIRE", []) == []


# ---------------------------------------------------------------------------
# Strict context, risk and upstream direction
# ---------------------------------------------------------------------------

def test_buy_immediate_pattern_rejected_when_context_below_vwap_and_ema50():
    result = evaluate_trade_entry(
        "BADBUYCTX",
        make_df([candle(99.0, 101.02, 98.98, 101.0)], baseline=105.0),
        "BUY",
        EQUITY,
        TICK,
        CandlestickEngine(CFG),
    )
    assert result.state == GateState.NO_PATTERN
    assert result.plan is None


def test_sell_immediate_pattern_rejected_when_context_above_vwap_and_ema50():
    result = evaluate_trade_entry(
        "BADSELLCTX",
        make_df([candle(101.0, 101.02, 98.98, 99.0)], baseline=95.0),
        "SELL",
        EQUITY,
        TICK,
        CandlestickEngine(CFG),
    )
    assert result.state == GateState.NO_PATTERN
    assert result.plan is None


def test_position_size_caps_risk_at_point_two_percent():
    qty = position_size(EQUITY, 100.0, 99.5, 0.20)
    assert qty == 20
    assert abs(100.0 - 99.5) * qty == pytest.approx(10.0)


def test_trade_plan_enforces_two_r_and_max_ten_rupee_planned_risk():
    setup = Setup(Pattern.HAMMER, Side.BUY, Trigger.BREAKOUT, 20, (20,), 100.55, 99.50, "risk regression")
    plan = build_trade_plan(setup, 21, 100.60, EQUITY, CFG)
    assert plan is not None
    assert plan.rr >= 2.0
    assert plan.planned_risk <= 10.0 + 1e-9
    expected_target = plan.entry_price + 2.0 * (plan.entry_price - plan.stop_price)
    assert plan.target_price == pytest.approx(expected_target)


def test_master_gate_never_overrides_upstream_block():
    result = evaluate_trade_entry(
        "BLOCKED",
        make_df([candle(100.0, 102.02, 99.98, 102.0)]),
        "BLOCK",
        EQUITY,
        TICK,
        CandlestickEngine(CFG),
    )
    assert result.state == GateState.NO_PATTERN
    assert result.plan is None
    assert result.setup is None
    assert result.reason == "INVALID_OR_BLOCKED_DIRECTION"
