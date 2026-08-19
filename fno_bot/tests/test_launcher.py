"""
Covers the bug caught by actually running launcher.py for the first
time: main() calling run_strike_and_signal() directly after PREPARE
without ever firing MARKET_OPEN_REACHED, leaving the state machine
stuck in WAIT_MARKET for the rest of the session (every later
transition() call became a silent no-op, so ENTRY_PENDING could never
be reached -- no PAPER/LIVE entry would ever fire, and ABORTED could
never be reached either -- shadow observation would never run).

wait_for_market_open() is the fix; these tests use injected
clock_fn/sleep_fn so nothing here actually sleeps in real time.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.state_machine import SessionContext, State, transition
from fno_bot.launcher import wait_for_market_open

IST = ZoneInfo("Asia/Kolkata")


def _ctx_at_wait_market():
    ctx = SessionContext()
    ctx = transition(ctx, "CLEAN_STARTUP")
    ctx = transition(ctx, "PREPARE_OK")
    assert ctx.state == State.WAIT_MARKET
    return ctx


def test_wait_for_market_open_transitions_immediately_when_already_open():
    ctx = _ctx_at_wait_market()
    already_open = datetime(2026, 8, 20, 9, 20, 0, tzinfo=IST)
    sleeps = []

    result = wait_for_market_open(ctx, clock_fn=lambda: already_open, sleep_fn=sleeps.append)

    assert result.state == State.FIRST_TICK_CAPTURE
    assert sleeps == []  # never had to wait -- already past market open


def test_wait_for_market_open_polls_until_open_time_reached():
    ctx = _ctx_at_wait_market()
    # Fake clock: starts before market open, advances by 60s on every
    # sleep_fn call, so the loop exits after a bounded number of polls.
    clock_state = {"now": datetime(2026, 8, 20, 9, 13, 0, tzinfo=IST)}

    def fake_clock():
        return clock_state["now"]

    def fake_sleep(seconds):
        clock_state["now"] = clock_state["now"].replace(minute=clock_state["now"].minute + 1) \
            if clock_state["now"].minute < 59 else clock_state["now"]

    result = wait_for_market_open(ctx, clock_fn=fake_clock, sleep_fn=fake_sleep, poll_seconds=1.0)

    assert result.state == State.FIRST_TICK_CAPTURE
    assert clock_state["now"].hour == 9 and clock_state["now"].minute >= 15


def test_first_tick_capture_market_invalid_goes_to_aborted():
    """The other half of tonight's bug: an early-timeout return from
    run_strike_and_signal() fires MARKET_INVALID while still formally
    in FIRST_TICK_CAPTURE (before FIRST_TICKS_CAPTURED has fired) --
    state_machine.py must recognize that, not silently ignore it."""
    ctx = SessionContext()
    ctx.state = State.FIRST_TICK_CAPTURE
    ctx = transition(ctx, "MARKET_INVALID", reason="no underlying tick within entry window")
    assert ctx.state == State.ABORTED
    assert ctx.abort_reason == "no underlying tick within entry window"
