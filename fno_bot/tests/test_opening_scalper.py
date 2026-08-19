from fno_bot.strategies.opening_scalper import evaluate_signals, evaluate_exit_conditions
from fno_bot.strategies.signal_candidates import MarketSnapshot


def _snapshot():
    return MarketSnapshot(
        underlying_price=77218.05, underlying_prev_close=77000.0,
        ce_price=307.30, pe_price=196.80,
        ce_best_bid=307.0, ce_best_ask=307.6, ce_best_bid_qty=50, ce_best_ask_qty=40,
        pe_best_bid=196.5, pe_best_ask=197.0, pe_best_bid_qty=30, pe_best_ask_qty=60,
    )


def test_evaluate_signals_no_authorized_signal_never_trades():
    all_results, authorized = evaluate_signals(_snapshot(), authorized_signal=None)
    assert authorized is None
    assert len(all_results) == 5  # still evaluated in shadow


def test_evaluate_signals_authorized_signal_surfaces_its_result():
    all_results, authorized = evaluate_signals(_snapshot(), authorized_signal="premium_imbalance")
    assert authorized is not None
    assert authorized.candidate == "premium_imbalance"
    assert authorized.direction == "PE"


def test_evaluate_signals_authorized_but_no_opinion_still_none():
    all_results, authorized = evaluate_signals(
        _snapshot(), authorized_signal="premium_rate_of_change"  # needs history, has none here
    )
    assert authorized is None


# --- exit hierarchy priority order (spec #15) ---

def _base_exit_kwargs(**overrides):
    base = dict(
        direction="PE", entry_price=205.86, current_price=205.86,
        target_pct=10.0, stop_loss_pct=5.0, held_seconds=10.0,
        max_hold_seconds=90.0, signal_still_valid=True, past_force_square_off=False,
    )
    base.update(overrides)
    return base


def test_emergency_takes_priority_over_everything():
    result = evaluate_exit_conditions(**_base_exit_kwargs(
        emergency_condition=True, emergency_reason="SPREAD_BLOWOUT",
        current_price=300.0,  # would also be a target hit
    ))
    assert result.should_exit
    assert result.reason == "SPREAD_BLOWOUT"


def test_hard_stop_loss_uses_actual_fill_not_reference_price():
    # entry_price is the ACTUAL fill (spec #18), stop = 205.86 * 0.95 = 195.567
    result = evaluate_exit_conditions(**_base_exit_kwargs(current_price=195.0))
    assert result.should_exit
    assert result.reason == "HARD_STOP_LOSS"


def test_signal_invalidation_before_target_even_if_profitable():
    result = evaluate_exit_conditions(**_base_exit_kwargs(
        current_price=230.0, signal_still_valid=False,
    ))
    assert result.should_exit
    assert result.reason == "SIGNAL_INVALIDATION"


def test_profit_target_fires_from_actual_fill():
    target = 205.86 * 1.10
    result = evaluate_exit_conditions(**_base_exit_kwargs(current_price=target + 0.01))
    assert result.should_exit
    assert result.reason == "PROFIT_TARGET"


def test_time_stop_fires_when_neither_target_nor_stop_hit():
    result = evaluate_exit_conditions(**_base_exit_kwargs(
        current_price=206.0, held_seconds=95.0, max_hold_seconds=90.0,
    ))
    assert result.should_exit
    assert result.reason == "TIME_STOP"


def test_end_of_session_mandatory_exit_lowest_priority():
    result = evaluate_exit_conditions(**_base_exit_kwargs(
        current_price=206.0, held_seconds=10.0, past_force_square_off=True,
    ))
    assert result.should_exit
    assert result.reason == "END_OF_SESSION_MANDATORY_EXIT"


def test_no_exit_when_nothing_fires():
    result = evaluate_exit_conditions(**_base_exit_kwargs(current_price=206.0))
    assert result.should_exit is False
    assert result.reason is None
