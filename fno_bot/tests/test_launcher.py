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


# ---------------------------------------------------------------------
# run_entry_window -- opening-range entry protocol (2026-08-20).
#
# Added after the first real PAPER session: the bot correctly declined
# a bad-price entry off the very first post-open tick (spread too wide,
# then slippage too high) and, because the original design only ever
# evaluated ONE tick, that was the end of its opportunity for the whole
# day. These tests cover the replacement: keep polling fresh ticks
# until either something fills or the entry window (ENTRY_END_TIME)
# expires.
#
# run_entry()/execute_entry()'s own internals (order submission,
# retries, kill-switch, sizing) are already covered by test_entry.py
# and test_risk_manager.py -- these tests monkeypatch run_entry and
# evaluate_signals to isolate exactly what's new here: the polling
# loop's control flow.
# ---------------------------------------------------------------------
from dataclasses import dataclass as _dataclass
from datetime import date as _date

from fno_bot import launcher as launcher_module
from fno_bot.market_data.tick_store import TickStore, NormalizedTick
from fno_bot.strategies.signal_candidates import DirectionSignal


@_dataclass
class _FakeContract:
    tradingsymbol: str
    exchange: str
    instrument_token: int
    lot_size: int = 20


@_dataclass
class _FakeSelection:
    underlying_price: float
    strike_interval: int
    atm_strike: float
    expiry: object
    ce_contract: _FakeContract
    pe_contract: _FakeContract


def _make_selection():
    return _FakeSelection(
        underlying_price=77000.0, strike_interval=100, atm_strike=77000.0, expiry=_date(2026, 8, 20),
        ce_contract=_FakeContract("SENSEX26820CE", "BFO", 111),
        pe_contract=_FakeContract("SENSEX26820PE", "BFO", 222),
    )


def _seed_ticks(tick_store, underlying_token=1, ce_token=111, pe_token=222):
    tick_store.update(NormalizedTick(underlying_token, 77000.0, None, None, None, None, None, None, 0.0))
    tick_store.update(NormalizedTick(ce_token, 100.0, 99.0, 101.0, 10, 10, None, None, 0.0))
    tick_store.update(NormalizedTick(pe_token, 200.0, 199.0, 201.0, 10, 10, None, None, 0.0))


def _authorized_signal():
    return DirectionSignal(candidate="premium_imbalance", direction="CE", confidence=100.0,
                            reason="test", raw_metrics={})


def _ctx_at_generate_signal():
    ctx = SessionContext()
    ctx.state = State.GENERATE_SIGNAL
    return ctx


def test_run_entry_window_stops_on_first_successful_fill(monkeypatch):
    tick_store = TickStore(clock_fn=lambda: 0.0)
    _seed_ticks(tick_store)
    selection = _make_selection()

    calls = {"n": 0}

    def fake_run_entry(broker, cfg_ref, ts, sel, authorized, ticker=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return None, None, "SPREAD_TOO_WIDE (spread_pct=6.0)"  # first two rounds: no fill, keep polling
        return object(), object(), None  # third round: fills

    monkeypatch.setattr(launcher_module, "run_entry", fake_run_entry)
    monkeypatch.setattr(launcher_module, "evaluate_signals",
                         lambda snapshot, authorized_name: ([_authorized_signal()], _authorized_signal()))

    # Clock never reaches window_end -- proves the loop stopped because
    # of the fill, not because it ran out of time.
    times = iter([datetime(2026, 8, 20, 9, 15, s, tzinfo=IST) for s in range(0, 30)])
    clock_fn = lambda: next(times)
    sleeps = []

    ctx, position, kill_switch, last_snapshot = launcher_module.run_entry_window(
        _ctx_at_generate_signal(), broker=object(), tick_store=tick_store, selection=selection,
        underlying_token=1, prev_close=76500.0, clock_fn=clock_fn, sleep_fn=sleeps.append, poll_seconds=1.0,
    )

    assert calls["n"] == 3  # stopped immediately on the fill, no 4th call
    assert position is not None
    assert kill_switch is not None
    assert ctx.state == State.POSITION_OPEN
    assert len(sleeps) == 2  # slept between the two failed rounds, not after the fill


def test_run_entry_window_times_out_cleanly_with_no_trade(monkeypatch):
    tick_store = TickStore(clock_fn=lambda: 0.0)
    _seed_ticks(tick_store)
    selection = _make_selection()

    calls = {"n": 0}

    def fake_run_entry(broker, cfg_ref, ts, sel, authorized, ticker=None):
        calls["n"] += 1
        return None, None, "SPREAD_TOO_WIDE (spread_pct=6.0)"  # never fills, ever

    monkeypatch.setattr(launcher_module, "run_entry", fake_run_entry)
    monkeypatch.setattr(launcher_module, "evaluate_signals",
                         lambda snapshot, authorized_name: ([_authorized_signal()], _authorized_signal()))

    # Fake clock crosses window_end (09:20:00) after a few polls.
    clock_state = {"now": datetime(2026, 8, 20, 9, 15, 1, tzinfo=IST)}

    def fake_clock():
        return clock_state["now"]

    def fake_sleep(seconds):
        clock_state["now"] = clock_state["now"].replace(
            minute=clock_state["now"].minute + 1, second=0
        )

    ctx, position, kill_switch, last_snapshot = launcher_module.run_entry_window(
        _ctx_at_generate_signal(), broker=object(), tick_store=tick_store, selection=selection,
        underlying_token=1, prev_close=76500.0, clock_fn=fake_clock, sleep_fn=fake_sleep, poll_seconds=1.0,
    )

    assert position is None
    assert calls["n"] >= 1  # it did try, repeatedly, before giving up
    assert ctx.state == State.ABORTED
    assert "WINDOW_EXPIRED_NO_TRADE" in ctx.abort_reason
    assert last_snapshot is not None  # available for shadow observation afterward
