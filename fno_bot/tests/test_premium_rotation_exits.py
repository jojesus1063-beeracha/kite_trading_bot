"""Tests for premium_rotation_exits.py -- the 6-mechanism exit system."""
from fno_bot.strategies.premium_rotation_exits import (
    OpenPosition, ExitParams, evaluate_exit, check_hard_stop, check_profit_target,
    check_trailing_stop, check_time_stop, check_session_cutoff, check_momentum_reversal,
)
from fno_bot.strategies.premium_rotation import WindowFeatures, RotationParams


def _features(ce_mom, pe_mom, vel, underlying_mom=0.0):
    return WindowFeatures(
        window_seconds=1.0, elapsed_seconds=1.0,
        ce_momentum_pct=ce_mom, pe_momentum_pct=pe_mom, underlying_momentum_pct=underlying_mom,
        difference_velocity=vel, premium_difference=0.0, premium_ratio=1.0, ratio_change=0.0,
    )


def test_hard_stop_fires_at_threshold():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=100.0)
    params = ExitParams(stop_loss_points=15.0)
    assert check_hard_stop(pos, current_price=84.0, params=params) is not None
    assert check_hard_stop(pos, current_price=86.0, params=params) is None


def test_profit_target_fires_at_threshold():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=100.0)
    params = ExitParams(profit_target_points=15.0)
    assert check_profit_target(pos, current_price=116.0, params=params) is not None
    assert check_profit_target(pos, current_price=114.0, params=params) is None


def test_momentum_reversal_fires_on_ce_position():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=108.0)
    params_rot = RotationParams()
    bad = _features(ce_mom=-3.0, pe_mom=4.0, vel=-8.0)
    assert check_momentum_reversal(pos, bad, params_rot) is not None


def test_momentum_reversal_silent_on_healthy_position():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=104.0)
    params_rot = RotationParams()
    good = _features(ce_mom=2.0, pe_mom=-1.0, vel=3.0)
    assert check_momentum_reversal(pos, good, params_rot) is None


def test_momentum_reversal_mirrors_correctly_for_pe():
    """A PE position should reverse-exit on the OPPOSITE pattern (PE
    collapsing, CE strengthening) -- not the same condition as CE."""
    pos = OpenPosition("PE", entry_price=100.0, entry_time=0.0, peak_favorable_price=108.0)
    params_rot = RotationParams()
    bad_for_pe = _features(ce_mom=4.0, pe_mom=-3.0, vel=8.0)
    assert check_momentum_reversal(pos, bad_for_pe, params_rot) is not None
    # the CE-collapse pattern must NOT trigger a PE position's reversal check
    bad_for_ce_only = _features(ce_mom=-3.0, pe_mom=4.0, vel=-8.0)
    assert check_momentum_reversal(pos, bad_for_ce_only, params_rot) is None


def test_trailing_stop_not_armed_below_activation():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=108.0)
    params = ExitParams(trailing_activation_points=20.0, trailing_distance_points=8.0)
    assert check_trailing_stop(pos, current_price=102.0, params=params) is None


def test_trailing_stop_fires_after_pullback_from_peak():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=130.0)
    params = ExitParams(trailing_activation_points=20.0, trailing_distance_points=8.0)
    # trail level = 130 - 8 = 122
    assert check_trailing_stop(pos, current_price=121.0, params=params) is not None
    assert check_trailing_stop(pos, current_price=125.0, params=params) is None


def test_time_stop_fires_when_stagnant():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=102.0)
    params = ExitParams(time_stop_seconds=90.0, time_stop_min_progress_points=5.0)
    assert check_time_stop(pos, current_price=101.0, now_time=95.0, params=params) is not None


def test_time_stop_silent_if_progress_made():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=110.0)
    params = ExitParams(time_stop_seconds=90.0, time_stop_min_progress_points=5.0)
    assert check_time_stop(pos, current_price=108.0, now_time=95.0, params=params) is None


def test_time_stop_silent_before_window_elapses():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=101.0)
    params = ExitParams(time_stop_seconds=90.0)
    assert check_time_stop(pos, current_price=100.5, now_time=30.0, params=params) is None


def test_session_cutoff():
    params = ExitParams(session_cutoff_hhmm="15:15")
    assert check_session_cutoff("15:16", params) is not None
    assert check_session_cutoff("15:10", params) is None


def test_priority_order_hard_stop_wins_over_everything():
    """Even if profit target AND trailing conditions would also fire,
    hard stop -- being a LOSS -- can never coexist with a profit
    condition, so this mainly proves hard stop is checked first."""
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=100.0)
    params_exit = ExitParams(stop_loss_points=15.0)
    params_rot = RotationParams()
    reason = evaluate_exit(pos, current_price=80.0, features=None, params_rotation=params_rot,
                            params_exit=params_exit, now_time=200.0, now_hhmm="15:20")
    assert reason is not None and "HARD_STOP" in reason   # not TIME_STOP or SESSION_CUTOFF, despite both also applying


def test_no_exit_when_nothing_applies():
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=104.0)
    params_exit = ExitParams()
    params_rot = RotationParams()
    good = _features(ce_mom=2.0, pe_mom=-1.0, vel=3.0)
    reason = evaluate_exit(pos, current_price=104.0, features=good, params_rotation=params_rot,
                            params_exit=params_exit, now_time=10.0, now_hhmm="10:00")
    assert reason is None


def test_momentum_reversal_skipped_when_features_unavailable():
    """features=None must never be silently treated as 'no reversal' in
    a way that masks a genuine problem -- it's simply not evaluated,
    and the caller relies on other checks (hard stop, time stop) as
    the safety net for that tick."""
    pos = OpenPosition("CE", entry_price=100.0, entry_time=0.0, peak_favorable_price=104.0)
    params_exit = ExitParams(stop_loss_points=15.0)
    params_rot = RotationParams()
    reason = evaluate_exit(pos, current_price=104.0, features=None, params_rotation=params_rot,
                            params_exit=params_exit, now_time=10.0, now_hhmm="10:00")
    assert reason is None   # no other condition fires either, correctly
