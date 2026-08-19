from fno_bot.state_machine import SessionContext, State, transition


def test_full_happy_path():
    ctx = SessionContext()
    ctx = transition(ctx, "CLEAN_STARTUP")
    assert ctx.state == State.PREPARE
    ctx = transition(ctx, "PREPARE_OK")
    assert ctx.state == State.WAIT_MARKET
    ctx = transition(ctx, "MARKET_OPEN_REACHED")
    assert ctx.state == State.FIRST_TICK_CAPTURE
    ctx = transition(ctx, "FIRST_TICKS_CAPTURED")
    assert ctx.state == State.VALIDATE_MARKET
    ctx = transition(ctx, "MARKET_VALID")
    assert ctx.state == State.SELECT_STRIKE
    ctx = transition(ctx, "STRIKE_SELECTED", selection="fake-selection")
    assert ctx.state == State.GENERATE_SIGNAL
    assert ctx.strike_selection == "fake-selection"
    ctx = transition(ctx, "SIGNAL_AUTHORIZED", signal="fake-signal")
    assert ctx.state == State.ENTRY_PENDING
    ctx = transition(ctx, "ENTRY_FILLED", result="fake-entry")
    assert ctx.state == State.POSITION_OPEN
    ctx = transition(ctx, "MONITORING_STARTED")
    assert ctx.state == State.MONITOR
    ctx = transition(ctx, "EXIT_CONDITION_FIRED")
    assert ctx.state == State.EXIT_PENDING
    ctx = transition(ctx, "EXIT_FILLED", result="fake-exit")
    assert ctx.state == State.CLOSED
    ctx = transition(ctx, "REPORTED")
    assert ctx.state == State.REPORT


def test_boot_recovery_path():
    ctx = SessionContext()
    ctx = transition(ctx, "RECOVERY_REQUIRED", plan="fake-plan")
    assert ctx.state == State.RECOVERY
    assert ctx.recovery_plan == "fake-plan"
    ctx = transition(ctx, "RECOVERY_RESOLVED")
    assert ctx.state == State.PREPARE


def test_recovery_failure_stops():
    ctx = SessionContext()
    ctx = transition(ctx, "RECOVERY_REQUIRED", plan="fake-plan")
    ctx = transition(ctx, "RECOVERY_FAILED", reason="could not reconcile")
    assert ctx.state == State.STOPPED
    assert ctx.abort_reason == "could not reconcile"


def test_market_validation_failure_aborts():
    ctx = SessionContext(state=State.VALIDATE_MARKET)
    ctx = transition(ctx, "MARKET_INVALID", reason="stale underlying tick")
    assert ctx.state == State.ABORTED
    assert ctx.abort_reason == "stale underlying tick"


def test_no_tradable_signal_aborts_this_attempt():
    ctx = SessionContext(state=State.GENERATE_SIGNAL)
    ctx = transition(ctx, "NO_TRADABLE_SIGNAL", reason="no authorized candidate fired")
    assert ctx.state == State.ABORTED


def test_entry_abort_preserves_reason():
    ctx = SessionContext(state=State.ENTRY_PENDING)
    ctx = transition(ctx, "ENTRY_ABORTED", reason="MAX_SLIPPAGE_EXCEEDED", result="fake-entry-result")
    assert ctx.state == State.ABORTED
    assert ctx.abort_reason == "MAX_SLIPPAGE_EXCEEDED"
    assert ctx.entry_result == "fake-entry-result"


def test_disconnected_while_flat_stops():
    ctx = SessionContext(state=State.WAIT_MARKET)
    ctx = transition(ctx, "DISCONNECTED_WHILE_FLAT")
    assert ctx.state == State.STOPPED


def test_partial_exit_stays_in_exit_pending_for_retry():
    ctx = SessionContext(state=State.EXIT_PENDING)
    ctx = transition(ctx, "EXIT_PARTIAL_REMAINING", result="fake-partial")
    assert ctx.state == State.EXIT_PENDING  # ladder must be retried, not silently accepted
    assert ctx.exit_result == "fake-partial"


def test_terminal_states_reject_further_transitions():
    ctx = SessionContext(state=State.STOPPED)
    ctx2 = transition(ctx, "ANYTHING")
    assert ctx2.state == State.STOPPED  # no-op, not a crash

    ctx = SessionContext(state=State.ABORTED)
    ctx2 = transition(ctx, "ANYTHING")
    assert ctx2.state == State.ABORTED
