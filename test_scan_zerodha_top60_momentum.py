from scan_zerodha_top60_momentum import (
    dedupe_same_symbol,
    directional_open_extreme_bonus,
    movement_gate,
    previous_day_bonus,
)


def test_movement_gate_accepts_change_threshold():
    assert movement_gate(0.30, 0.10, 0.30, 0.40)
    assert movement_gate(-0.30, 0.10, 0.30, 0.40)


def test_movement_gate_accepts_range_threshold():
    assert movement_gate(0.05, 0.40, 0.30, 0.40)


def test_movement_gate_rejects_quiet_stock():
    assert not movement_gate(0.29, 0.39, 0.30, 0.40)


def test_bullish_open_equals_low_gets_bonus():
    points, reason = directional_open_extreme_bonus(
        1.0, 100.0, 102.0, 100.0, 0.05, 0
    )
    assert points == 5.0
    assert reason == "BULLISH_OPEN_EQUALS_LOW"


def test_bearish_open_equals_high_gets_bonus():
    points, reason = directional_open_extreme_bonus(
        -1.0, 100.0, 100.0, 98.0, 0.05, 0
    )
    assert points == 5.0
    assert reason == "BEARISH_OPEN_EQUALS_HIGH"


def test_wrong_direction_open_extreme_gets_no_bonus():
    points, reason = directional_open_extreme_bonus(
        -1.0, 100.0, 102.0, 100.0, 0.05, 0
    )
    assert points == 0.0
    assert reason == "NONE"


def test_same_symbol_dedupe_keeps_more_liquid_listing():
    rows = [
        {"symbol": "ABC", "exchange": "NSE", "turnover": 2_000_000, "spread_pct": 0.10},
        {"symbol": "ABC", "exchange": "BSE", "turnover": 3_000_000, "spread_pct": 0.08},
    ]
    selected, removed = dedupe_same_symbol(rows)
    assert removed == 1
    assert len(selected) == 1
    assert selected[0]["exchange"] == "BSE"


def test_same_symbol_dedupe_prefers_nse_on_equal_quality():
    rows = [
        {"symbol": "ABC", "exchange": "BSE", "turnover": 3_000_000, "spread_pct": 0.08},
        {"symbol": "ABC", "exchange": "NSE", "turnover": 3_000_000, "spread_pct": 0.08},
    ]
    selected, removed = dedupe_same_symbol(rows)
    assert removed == 1
    assert selected[0]["exchange"] == "NSE"


def test_previous_day_bonus_is_capped_at_five_points():
    assert previous_day_bonus({"previous_day_momentum_score": 10.0}) == 5.0
    assert previous_day_bonus({"previous_day_momentum_score": 2.5}) == 2.5
    assert previous_day_bonus(None) == 0.0
