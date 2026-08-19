from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.monitoring.position_monitor import (
    init_excursion, update_excursion, is_past_force_square_off, compute_monitor_decision,
)

IST = ZoneInfo("Asia/Kolkata")


def test_init_and_update_excursion():
    state = init_excursion(entry_price=200.0)
    assert state.mfe_pct == 0.0
    assert state.mae_pct == 0.0

    state = update_excursion(state, 220.0)
    assert round(state.mfe_pct, 2) == 10.0
    assert state.mae_pct == 0.0  # never went below entry

    state = update_excursion(state, 190.0)
    assert round(state.mae_pct, 2) == -5.0
    assert round(state.mfe_pct, 2) == 10.0  # still remembers the earlier high


def test_is_past_force_square_off():
    before = datetime(2026, 8, 19, 15, 9, tzinfo=IST)
    at = datetime(2026, 8, 19, 15, 10, tzinfo=IST)
    after = datetime(2026, 8, 19, 15, 30, tzinfo=IST)
    assert is_past_force_square_off(before, "15:10") is False
    assert is_past_force_square_off(at, "15:10") is True
    assert is_past_force_square_off(after, "15:10") is True


class FakeCfg:
    TARGET_PCT = 10.0
    STOP_LOSS_PCT = 5.0
    MAX_HOLD_SECONDS = 90.0
    FORCE_SQUARE_OFF_TIME = "15:10"


def test_compute_monitor_decision_target_hit():
    excursion = init_excursion(205.86)
    result, updated = compute_monitor_decision(
        direction="PE", entry_price=205.86, current_price=230.0, excursion=excursion,
        held_seconds=5.0, signal_still_valid=True,
        now_ist=datetime(2026, 8, 19, 9, 16, tzinfo=IST), cfg=FakeCfg(),
    )
    assert result.should_exit
    assert result.reason == "PROFIT_TARGET"
    assert updated.mfe_pct > 0


def test_compute_monitor_decision_no_exit_mid_range():
    excursion = init_excursion(205.86)
    result, updated = compute_monitor_decision(
        direction="PE", entry_price=205.86, current_price=207.0, excursion=excursion,
        held_seconds=5.0, signal_still_valid=True,
        now_ist=datetime(2026, 8, 19, 9, 16, tzinfo=IST), cfg=FakeCfg(),
    )
    assert not result.should_exit


def test_compute_monitor_decision_force_square_off():
    excursion = init_excursion(205.86)
    result, updated = compute_monitor_decision(
        direction="PE", entry_price=205.86, current_price=207.0, excursion=excursion,
        held_seconds=5.0, signal_still_valid=True,
        now_ist=datetime(2026, 8, 19, 15, 12, tzinfo=IST), cfg=FakeCfg(),
    )
    assert result.should_exit
    assert result.reason == "END_OF_SESSION_MANDATORY_EXIT"


def test_compute_monitor_decision_emergency_overrides_everything():
    excursion = init_excursion(205.86)
    result, updated = compute_monitor_decision(
        direction="PE", entry_price=205.86, current_price=230.0, excursion=excursion,
        held_seconds=5.0, signal_still_valid=True,
        now_ist=datetime(2026, 8, 19, 9, 16, tzinfo=IST), cfg=FakeCfg(),
        emergency_condition=True, emergency_reason="SPREAD_BLOWOUT",
    )
    assert result.should_exit
    assert result.reason == "SPREAD_BLOWOUT"
