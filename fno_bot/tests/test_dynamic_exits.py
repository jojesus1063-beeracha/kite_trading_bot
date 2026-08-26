from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.monitoring.position_monitor import compute_monitor_decision, init_excursion
from fno_bot.strategies.dynamic_exits import build_dynamic_exit_plan

IST = ZoneInfo("Asia/Kolkata")


class Cfg:
    TARGET_PCT = 10.0
    STOP_LOSS_PCT = 5.0
    MAX_HOLD_SECONDS = 90
    FORCE_SQUARE_OFF_TIME = "15:10"


def _decision(price, excursion, plan):
    return compute_monitor_decision(
        direction="CE", entry_price=100, current_price=price,
        excursion=excursion, held_seconds=10, signal_still_valid=True,
        now_ist=datetime(2026, 8, 26, 10, 0, tzinfo=IST), cfg=Cfg(),
        risk_plan=plan,
    )


def test_dynamic_plan_is_bounded_and_preserves_minimum_reward_risk():
    plan = build_dynamic_exit_plan(
        [100, 101, 99, 102], momentum_pct=2.0, spread_pct=0.5,
        entry_price=100, quantity=100,
    )
    assert 3.0 <= plan.stop_pct <= 7.5
    assert 6.0 <= plan.target_pct <= 15.0
    assert plan.target_pct >= plan.stop_pct * 1.8


def test_dynamic_plan_caps_extreme_volatility():
    plan = build_dynamic_exit_plan(
        [100, 130, 80, 120], momentum_pct=20, spread_pct=4,
        entry_price=100, quantity=100,
    )
    assert plan.stop_pct == 7.5
    assert plan.target_pct == 15.0


def test_breakeven_stop_never_widens_initial_stop():
    plan = build_dynamic_exit_plan(
        [100, 100.5], momentum_pct=1, spread_pct=0.2,
        entry_price=100, quantity=100,
    )
    excursion = init_excursion(100)
    _, excursion = _decision(104, excursion, plan)
    result, _ = _decision(100, excursion, plan)
    assert result.should_exit
    assert result.reason == "DYNAMIC_BREAKEVEN_STOP"


def test_trailing_stop_uses_high_water_mark():
    plan = build_dynamic_exit_plan(
        [100, 100.5], momentum_pct=1, spread_pct=0.2,
        entry_price=100, quantity=100,
    )
    excursion = init_excursion(100)
    _, excursion = _decision(106, excursion, plan)
    result, _ = _decision(103, excursion, plan)
    assert result.should_exit
    assert result.reason == "DYNAMIC_TRAILING_STOP"
