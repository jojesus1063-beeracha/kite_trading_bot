"""Tests for premium_rotation_session.py -- full lifecycle + counterfactuals."""
from fno_bot.strategies.premium_rotation import TickSample, RotationParams
from fno_bot.strategies.premium_rotation_gate import EntryParams
from fno_bot.strategies.premium_rotation_exits import ExitParams
from fno_bot.strategies.premium_rotation_session import ShadowSession, compute_counterfactual


def _make_session():
    params_rot = RotationParams(
        ce_momentum_min_pct=1.0, pe_weakness_max_pct=0.3, velocity_min=1.5,
        underlying_confirm_min_pct=0.005,
    )
    params_entry = EntryParams(score_threshold=60.0, dominance_margin=15.0, anti_chase_max_extension_pct=25.0)
    params_exit = ExitParams(stop_loss_points=15.0, profit_target_points=25.0,
                              trailing_activation_points=15.0, trailing_distance_points=6.0,
                              time_stop_seconds=90.0)
    return ShadowSession(params_rot, params_entry, params_exit, window_seconds=1.0, confirmation_required_count=3)


def test_full_lifecycle_opens_and_exits_on_momentum_reversal():
    session = _make_session()
    ce_seq =         [100, 101.5, 103.2, 105.1, 107.3, 109.8, 108.0, 104.0, 99.0]
    pe_seq =         [150, 148.8, 147.3, 145.6, 143.7, 141.5, 143.0, 147.0, 152.0]
    underlying_seq = [20000, 20003, 20007, 20011, 20016, 20021, 20020, 20014, 20005]

    for i in range(len(ce_seq)):
        session.on_tick(TickSample(float(i), ce_seq[i], pe_seq[i], underlying_seq[i]))

    assert len(session.closed_trades) == 1
    trade = session.closed_trades[0]
    assert trade.direction == "CE"
    assert trade.entry_price == 105.1
    assert "MOMENTUM_REVERSAL" in trade.exit_reason
    assert trade.exit_price > trade.entry_price   # closed profitably, before the later collapse to 99.0
    assert trade.mfe_points > 0   # captured the run-up along the way


def test_every_tick_produces_a_record_even_when_flat():
    """Section 16's mandatory logging requirement: flat-and-nothing-
    happened ticks must still be recorded, not skipped."""
    session = _make_session()
    ticks = [TickSample(0.0, 100, 150, 20000), TickSample(1.0, 100.1, 149.9, 20000.1)]
    for t in ticks:
        session.on_tick(t)
    assert len(session.records) == len(ticks)
    assert all(r is not None for r in session.records)


def test_no_position_no_crash_on_flat_session():
    session = _make_session()
    for i in range(5):
        session.on_tick(TickSample(float(i), 100 + i * 0.01, 150 - i * 0.01, 20000))
    assert session.open_position is None
    assert len(session.closed_trades) == 0


def test_counterfactual_tracks_forward_from_a_rejection_point():
    session = _make_session()
    ce_seq = [100, 101.5, 103.2, 105.1, 107.3]
    pe_seq = [150, 148.8, 147.3, 145.6, 143.7]
    underlying_seq = [20000, 20003, 20007, 20011, 20016]
    for i in range(len(ce_seq)):
        session.on_tick(TickSample(float(i), ce_seq[i], pe_seq[i], underlying_seq[i]))

    cf = compute_counterfactual(session.history, rejection_index=1, direction="CE", horizon_seconds=[2.0])
    assert 2.0 in cf.horizons
    assert cf.horizons[2.0]["max_favorable_pct"] > 0   # price did rise after t=1
    assert cf.reference_price == 101.5


def test_counterfactual_handles_insufficient_future_data():
    session = _make_session()
    session.on_tick(TickSample(0.0, 100, 150, 20000))
    cf = compute_counterfactual(session.history, rejection_index=0, direction="CE", horizon_seconds=[60.0])
    assert cf.horizons == {}   # no future data yet -- must not fabricate a result
