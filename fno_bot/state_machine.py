"""
The opening-trade state machine (architecture review, Section D),
wiring together every module built so far. This is the ONLY place
that owns transition logic between states -- individual modules never
decide what state comes next.

States (matching the architecture review exactly):
BOOT -> PREPARE -> WAIT_MARKET -> FIRST_TICK_CAPTURE -> VALIDATE_MARKET
-> SELECT_STRIKE -> GENERATE_SIGNAL -> ENTRY_PENDING -> POSITION_OPEN
-> MONITOR -> EXIT_PENDING -> CLOSED -> REPORT, with RECOVERY as an
extra state entered from BOOT when crash_recovery detects unexpected
broker state.

HONESTY ABOUT SCOPE: the state DEFINITIONS and TRANSITION LOGIC below
are complete and unit-testable (see tests/test_state_machine.py) using
injected fakes for the broker/ticker. The `main()` entrypoint at the
bottom that wires REAL KiteConnect/KiteTicker objects together is
necessarily untested here (it needs a live broker connection to
exercise) and un-exercised end-to-end -- treat it as a starting point
for Phase 6 (PAPER mode) integration testing, not as validated
production code. PAPER-mode fill simulation and replay mode (spec
#23, #35) are NOT implemented yet -- SHADOW is the only mode this
currently supports meaningfully end-to-end, matching cfg.MODE's
default and the phased plan.
"""
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any

logger = logging.getLogger("fno.state_machine")


class State(str, Enum):
    BOOT = "BOOT"
    RECOVERY = "RECOVERY"
    PREPARE = "PREPARE"
    WAIT_MARKET = "WAIT_MARKET"
    FIRST_TICK_CAPTURE = "FIRST_TICK_CAPTURE"
    VALIDATE_MARKET = "VALIDATE_MARKET"
    SELECT_STRIKE = "SELECT_STRIKE"
    GENERATE_SIGNAL = "GENERATE_SIGNAL"
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    MONITOR = "MONITOR"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    REPORT = "REPORT"
    ABORTED = "ABORTED"          # terminal, no trade this session (or this attempt)
    STOPPED = "STOPPED"          # terminal, e.g. WebSocket unrecoverable / disconnected-while-flat


@dataclass
class SessionContext:
    """All the mutable state threaded through the machine. Deliberately
    a plain dataclass (not hidden inside closures) so tests can inspect
    it directly after each transition."""
    state: State = State.BOOT
    recovery_plan: Optional[Any] = None
    strike_selection: Optional[Any] = None
    authorized_signal_result: Optional[Any] = None
    entry_result: Optional[Any] = None
    excursion: Optional[Any] = None
    exit_result: Optional[Any] = None
    abort_reason: Optional[str] = None
    trade_record: dict = field(default_factory=dict)


def transition(ctx: SessionContext, event: str, **data) -> SessionContext:
    """
    Pure-ish transition function: given the current context and a
    named event (produced by the orchestration loop after doing the
    actual I/O for the current state), returns the NEXT context. Holds
    no I/O itself -- every branch here is a plain state-machine
    decision, which is what makes this testable without a broker.
    """
    s = ctx.state

    if s == State.BOOT:
        if event == "RECOVERY_REQUIRED":
            ctx.recovery_plan = data.get("plan")
            ctx.state = State.RECOVERY
        elif event == "CLEAN_STARTUP":
            ctx.state = State.PREPARE
        return ctx

    if s == State.RECOVERY:
        if event == "RECOVERY_RESOLVED":
            ctx.state = State.PREPARE
        elif event == "RECOVERY_FAILED":
            ctx.abort_reason = data.get("reason", "recovery failed")
            ctx.state = State.STOPPED
        return ctx

    if s == State.PREPARE:
        if event == "PREPARE_OK":
            ctx.state = State.WAIT_MARKET
        elif event == "PREPARE_FAILED":
            ctx.abort_reason = data.get("reason", "prepare failed")
            ctx.state = State.STOPPED
        return ctx

    if s == State.WAIT_MARKET:
        if event == "MARKET_OPEN_REACHED":
            ctx.state = State.FIRST_TICK_CAPTURE
        elif event == "DISCONNECTED_WHILE_FLAT":
            ctx.state = State.STOPPED
            ctx.abort_reason = "disconnected while flat before market open"
        return ctx

    if s == State.FIRST_TICK_CAPTURE:
        if event == "FIRST_TICKS_CAPTURED":
            ctx.state = State.VALIDATE_MARKET
        elif event == "MARKET_INVALID":
            # No first tick arrived at all within the entry window --
            # same terminal-for-this-attempt outcome as failing
            # validation after a tick DID arrive (VALIDATE_MARKET's own
            # MARKET_INVALID branch below), just detected one step
            # earlier in the sequence.
            ctx.abort_reason = data.get("reason", "no first tick within entry window")
            ctx.state = State.ABORTED
        return ctx

    if s == State.VALIDATE_MARKET:
        if event == "MARKET_VALID":
            ctx.state = State.SELECT_STRIKE
        elif event == "MARKET_INVALID":
            ctx.abort_reason = data.get("reason", "market validation failed")
            ctx.state = State.ABORTED
        return ctx

    if s == State.SELECT_STRIKE:
        if event == "STRIKE_SELECTED":
            ctx.strike_selection = data.get("selection")
            ctx.state = State.GENERATE_SIGNAL
        elif event == "STRIKE_SELECTION_FAILED":
            ctx.abort_reason = data.get("reason", "no valid ATM strike")
            ctx.state = State.ABORTED
        return ctx

    if s == State.GENERATE_SIGNAL:
        if event == "SIGNAL_AUTHORIZED":
            ctx.authorized_signal_result = data.get("signal")
            ctx.state = State.ENTRY_PENDING
        elif event == "NO_TRADABLE_SIGNAL":
            # Not necessarily terminal for the SESSION (spec: keep observing
            # for MAX_ENTRY_WINDOW_SECONDS), but terminal for THIS attempt.
            ctx.abort_reason = data.get("reason", "no authorized signal fired")
            ctx.state = State.ABORTED
        return ctx

    if s == State.ENTRY_PENDING:
        if event == "ENTRY_FILLED":
            ctx.entry_result = data.get("result")
            ctx.state = State.POSITION_OPEN
        elif event == "ENTRY_ABORTED" or event == "ENTRY_NO_FILL":
            ctx.abort_reason = data.get("reason", "entry not filled")
            ctx.entry_result = data.get("result")
            ctx.state = State.ABORTED
        return ctx

    if s == State.POSITION_OPEN:
        if event == "MONITORING_STARTED":
            ctx.state = State.MONITOR
        return ctx

    if s == State.MONITOR:
        if event == "EXIT_CONDITION_FIRED":
            ctx.state = State.EXIT_PENDING
        elif event == "DISCONNECTED_WHILE_OPEN_TIMEOUT":
            # Still must exit -- emergency handling routes through EXIT_PENDING too,
            # just with action=FORCE_EXIT and an emergency price, per exit.py.
            ctx.state = State.EXIT_PENDING
        return ctx

    if s == State.EXIT_PENDING:
        if event == "EXIT_FILLED":
            ctx.exit_result = data.get("result")
            ctx.state = State.CLOSED
        elif event == "EXIT_PARTIAL_REMAINING":
            # Ladder exhausted with SOME fill but not all -- stays in
            # EXIT_PENDING; the orchestration loop re-attempts the ladder
            # for the remaining quantity on the next cycle.
            ctx.exit_result = data.get("result")
        return ctx

    if s == State.CLOSED:
        if event == "REPORTED":
            ctx.state = State.REPORT
        return ctx

    # ABORTED, STOPPED, REPORT are terminal for this run -- no further
    # transitions accepted (an orchestration bug calling transition()
    # again on a terminal state is a no-op, not a crash).
    return ctx
