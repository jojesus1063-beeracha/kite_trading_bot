"""Tests for matmon_post_di_freeze.py and its wiring into CLEAN/microstructure.

Core property under test: once EMA/DI passes at T0, the post-DI CLEAN
window's fate must depend only on the ticks that actually arrived in
[T0, T0+window_seconds] -- never on how much wall-clock time elapses before
the candidate is actually processed. The old live-query path failed this
property (see test_delayed_processing_loses_evidence_on_old_live_path,
which pins down the bug being fixed); the frozen-evidence path fixes it
(see test_frozen_evidence_survives_arbitrarily_delayed_processing).
"""
import threading
import time
from types import SimpleNamespace

from ws_ticker import TickBuffer
import matmon_post_di_freeze as freeze
from matmon_quote_confirmation import evaluate_quote_window
from matmon_microstructure import evaluate_microstructure


def _depth(base_bid=100.0, base_ask=100.1, bid_qty=120, ask_qty=80):
    return {
        "buy": [
            {"price": base_bid - i * 0.05, "quantity": bid_qty - i * 5}
            for i in range(5)
        ],
        "sell": [
            {"price": base_ask + i * 0.05, "quantity": ask_qty + i * 5}
            for i in range(5)
        ],
    }


def _tick(ts, bid, ask, ltp, *, bid_qty=120, ask_qty=80):
    return {
        "received_at": ts,
        "last_price": ltp,
        "depth": _depth(bid, ask, bid_qty, ask_qty),
    }


def _buffer(symbol, rows):
    b = TickBuffer()
    for row in rows:
        b.append(symbol, row)
    return b


def _clean_buy_ticks(t0):
    """A textbook CLEAN BUY window: bid/ask/ltp/imbalance all strengthen
    monotonically across [t0, t0+3]."""
    return [
        _tick(t0 + 0.0, 100.00, 100.10, 100.05, bid_qty=140, ask_qty=60),
        _tick(t0 + 1.0, 100.05, 100.15, 100.12, bid_qty=150, ask_qty=55),
        _tick(t0 + 2.0, 100.10, 100.20, 100.18, bid_qty=160, ask_qty=50),
        _tick(t0 + 3.0, 100.15, 100.25, 100.25, bid_qty=170, ask_qty=45),
    ]


def setup_function(_):
    freeze.reset_for_tests()


# ---------------------------------------------------------------------
# Direct regression: pin down the bug being fixed on the OLD live-query
# path, so this fix can never silently regress back to it.
# ---------------------------------------------------------------------

def test_delayed_processing_loses_evidence_on_old_live_path():
    t0 = 1_000_000.0
    ticks = _clean_buy_ticks(t0)
    buffer = _buffer("ABC", ticks)

    # Confirmation actually runs 10s after T0 -- a plausible real delay from
    # market-regime resolution, watchlist filters, RVOL/EMA-distance checks,
    # and a margin lookup all running for this symbol before confirmation.
    delayed_now = t0 + 10.0

    result = evaluate_quote_window(
        buffer, "ABC", "BUY",
        window_seconds=3.0, max_age_seconds=2.0,
        now=delayed_now, not_before=t0,
    )
    # The evidence was genuinely CLEAN when it arrived -- but the live path
    # rejects it because "now" (confirmation time) drifted far enough past
    # T0 that its own window/max_age lookback (now - window - max_age,
    # clamped to not_before) no longer reaches back to where the valid
    # ticks actually are. This is the bug: real CLEAN evidence, lost purely
    # to processing delay.
    assert result.available is False
    assert result.reason == "MATMON_NO_POST_DI_TICKS"


# ---------------------------------------------------------------------
# Frozen path: same evidence, same delay, correct outcome.
# ---------------------------------------------------------------------

def test_frozen_evidence_survives_arbitrarily_delayed_processing():
    t0 = 1_000_000.0
    ticks = _clean_buy_ticks(t0)
    buffer = _buffer("ABC", ticks)

    def fake_sleep(_seconds):
        pass  # instant in tests

    now_box = {"t": t0}

    def fake_now():
        return now_box["t"]

    entry = freeze.start_capture(
        "ABC", t0, tick_buffer=buffer, window_seconds=3.0,
        sleep_fn=fake_sleep, now_fn=fake_now,
    )
    # Let capture's target deadline (t0+3) appear to have already elapsed.
    now_box["t"] = t0 + 3.0
    entry.ready.wait(timeout=2.0)

    # Confirmation "runs" arbitrarily late -- 10s, 60s, doesn't matter.
    for delayed_now in (t0 + 10.0, t0 + 60.0):
        freeze.start_capture(  # re-register so pop doesn't consume across iterations
            "ABC", t0, tick_buffer=buffer, window_seconds=3.0,
            sleep_fn=fake_sleep, now_fn=lambda: t0 + 3.0,
        )
        frozen = freeze.pop_frozen_evidence("ABC", t0, wait_timeout=2.0)
        assert frozen is not None and len(frozen) == len(ticks)

        result = evaluate_quote_window(
            buffer, "ABC", "BUY",
            window_seconds=3.0, max_age_seconds=2.0,
            now=delayed_now, not_before=t0,
            frozen_ticks=frozen,
        )
        assert result.available is True
        assert result.confirmed is True, result.reason
        assert result.reason == "MATMON_QUOTE_CONFIRMED"

        # Microstructure downstream of CLEAN also unaffected by the delay.
        micro = evaluate_microstructure("BUY", result.ticks)
        assert micro.accepted is True


def test_pop_frozen_evidence_returns_none_when_no_capture_started():
    assert freeze.pop_frozen_evidence("NEVERCAPTURED", 123.0) is None


def test_pop_frozen_evidence_rejects_mismatched_di_passed_at():
    t0 = 2_000_000.0
    buffer = _buffer("XYZ", _clean_buy_ticks(t0))
    freeze.start_capture(
        "XYZ", t0, tick_buffer=buffer, window_seconds=3.0,
        sleep_fn=lambda s: None, now_fn=lambda: t0 + 3.0,
    )
    time.sleep(0.05)
    # A stale/newer signal for the same symbol must never be satisfied by an
    # entry captured for a different T0.
    assert freeze.pop_frozen_evidence("XYZ", t0 + 100.0) is None


def test_capture_only_freezes_ticks_inside_the_post_di_window():
    t0 = 3_000_000.0
    ticks = [
        _tick(t0 - 5.0, 99.0, 99.1, 99.05),   # pre-DI: must never count
        *_clean_buy_ticks(t0),                 # in-window
        _tick(t0 + 10.0, 101.0, 101.1, 101.05),  # long after window closes
    ]
    buffer = _buffer("PQR", ticks)
    freeze.start_capture(
        "PQR", t0, tick_buffer=buffer, window_seconds=3.0,
        sleep_fn=lambda s: None, now_fn=lambda: t0 + 3.0,
    )
    frozen = freeze.pop_frozen_evidence("PQR", t0, wait_timeout=2.0)
    assert frozen is not None
    timestamps = sorted(t["received_at"] for t in frozen)
    assert timestamps == [t0 + 0.0, t0 + 1.0, t0 + 2.0, t0 + 3.0]


def test_maybe_start_capture_is_noop_for_non_matmon_signal():
    signal = SimpleNamespace(confidence="OTHER_STRATEGY")
    cfg = SimpleNamespace(MATMON_MODE=True)
    ws_engine = SimpleNamespace(ws_ticker=SimpleNamespace(tick_buffer=_buffer("Z", [])))
    result = freeze.maybe_start_capture(
        "Z", signal, ws_engine=ws_engine, cfg=cfg, di_passed_at=time.time(),
    )
    assert result is None


def test_maybe_start_capture_is_noop_without_tick_infrastructure():
    signal = SimpleNamespace(confidence="MATMON_EMA_DI")
    cfg = SimpleNamespace(MATMON_MODE=True)
    result = freeze.maybe_start_capture(
        "Z", signal, ws_engine=None, cfg=cfg, di_passed_at=time.time(),
    )
    assert result is None


def test_maybe_start_capture_starts_for_matmon_signal_with_engine():
    signal = SimpleNamespace(confidence="MATMON_EMA_DI")
    cfg = SimpleNamespace(MATMON_MODE=True, MATMON_QUOTE_WINDOW_SECONDS=3.0)
    buffer = _buffer("Q", [_tick(500.0, 100.0, 100.1, 100.05)])
    ws_engine = SimpleNamespace(ws_ticker=SimpleNamespace(tick_buffer=buffer))
    entry = freeze.maybe_start_capture(
        "Q", signal, ws_engine=ws_engine, cfg=cfg, di_passed_at=500.0,
    )
    assert entry is not None
    entry.ready.wait(timeout=2.0)
    assert entry.reason in {"CAPTURED", "CAPTURE_EMPTY"}


def test_pop_frozen_evidence_times_out_gracefully_if_never_ready():
    """If capture is still in flight when confirmation asks for it, the
    caller gets None (and falls back to the live path) rather than hanging
    forever or crashing."""
    t0 = 4_000_000.0
    buffer = _buffer("SLOW", [])
    started = threading.Event()

    def blocking_sleep(_seconds):
        started.set()
        time.sleep(10)  # never returns within the test's wait_timeout

    entry = freeze.start_capture(
        "SLOW", t0, tick_buffer=buffer, window_seconds=3.0,
        sleep_fn=blocking_sleep, now_fn=lambda: t0,
    )
    started.wait(timeout=1.0)
    assert freeze.pop_frozen_evidence("SLOW", t0, wait_timeout=0.2) is None
