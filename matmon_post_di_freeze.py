#!/usr/bin/env python3
"""Freeze post-DI evidence at T0 so delayed candidate processing cannot lose it.

Matmon's EMA/DI direction step (T0) and its CLEAN/microstructure confirmation
step are separated by real per-symbol latency: market-regime resolution,
watchlist filters, RVOL/EMA-distance checks, and (in live mode) a broker
margin lookup all run in between, for the same symbol, before confirmation
executes. Before this module existed, confirmation re-queried the live tick
buffer relative to wall-clock "now" at whatever moment it actually ran, so a
delayed confirmation could see perfectly valid post-DI ticks rejected as
MATMON_STALE_QUOTE purely because "now" had moved on -- not because the
evidence was ever bad.

This module captures exactly the T0..T0+window_seconds tick batch for a
symbol in a background thread, starting the instant EMA/DI passes, and
freezes it. Confirmation -- whenever it actually runs -- consumes that
immutable snapshot instead of re-querying the live buffer against a moving
"now".

This module does not change EMA/DI, risk, quantity, or CLEAN/microstructure
thresholds. It only changes where the evidence for those unchanged checks
comes from, and pins *when* it was captured to T0 instead of "whenever
confirmation happened to run".
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("matmon_post_di_freeze")

DEFAULT_WINDOW_SECONDS = 3.0
DEFAULT_CAPTURE_POLL_SECONDS = 0.05
DEFAULT_WAIT_TIMEOUT_SECONDS = 4.0

_lock = threading.Lock()
_entries: dict[str, "_FrozenEntry"] = {}


class _FrozenEntry:
    __slots__ = ("symbol", "di_passed_at", "ready", "ticks", "reason")

    def __init__(self, symbol, di_passed_at):
        self.symbol = symbol
        self.di_passed_at = di_passed_at
        self.ready = threading.Event()
        self.ticks: tuple = ()
        self.reason = "CAPTURE_PENDING"


def _tick_within_window(tick, start, end):
    try:
        received_at = float((tick or {}).get("received_at"))
    except (TypeError, ValueError):
        return False
    return start <= received_at <= end


def _capture_worker(entry, tick_buffer, window_seconds, poll_seconds, sleep_fn, now_fn):
    deadline = entry.di_passed_at + window_seconds
    try:
        while True:
            remaining = deadline - now_fn()
            if remaining <= 0:
                break
            sleep_fn(min(poll_seconds, remaining))

        rows = tick_buffer.ticks_received_since(entry.symbol, entry.di_passed_at)
        frozen = tuple(
            tick for tick in (rows or ())
            if _tick_within_window(tick, entry.di_passed_at, deadline)
        )
        entry.ticks = frozen
        entry.reason = "CAPTURED" if frozen else "CAPTURE_EMPTY"
    except Exception:  # pragma: no cover - defensive; never crash the scan loop
        logger.exception("MATMON_FREEZE_CAPTURE_FAILED | %s", entry.symbol)
        entry.reason = "CAPTURE_FAILED"
    finally:
        entry.ready.set()


def start_capture(symbol, di_passed_at, *, tick_buffer,
                   window_seconds=DEFAULT_WINDOW_SECONDS,
                   poll_seconds=DEFAULT_CAPTURE_POLL_SECONDS,
                   sleep_fn=time.sleep, now_fn=time.time,
                   thread_factory=threading.Thread):
    """Begin freezing ticks for ``symbol`` from ``di_passed_at`` to +window_seconds.

    Entries are keyed by symbol and stamped with ``di_passed_at``, so a new
    signal for the same symbol always registers a fresh entry and a stale
    capture can never satisfy a newer signal's confirmation (``pop_frozen_evidence``
    checks the timestamp matches before returning anything).
    """
    if tick_buffer is None or di_passed_at is None:
        return None

    entry = _FrozenEntry(symbol, float(di_passed_at))
    with _lock:
        _entries[symbol] = entry

    thread = thread_factory(
        target=_capture_worker,
        args=(entry, tick_buffer, float(window_seconds), float(poll_seconds), sleep_fn, now_fn),
        daemon=True,
        name=f"matmon-freeze-{symbol}",
    )
    thread.start()
    return entry


def maybe_start_capture(symbol, signal, *, ws_engine, cfg, di_passed_at, window_seconds=None):
    """Convenience hook for the scan loop: no-op unless this is a fresh Matmon
    EMA/DI signal and live tick infrastructure is available. Safe to call
    unconditionally for every strategy's signal -- it only acts on Matmon's.
    """
    if signal is None or di_passed_at is None:
        return None
    if str(getattr(signal, "confidence", "")) != "MATMON_EMA_DI":
        return None
    if not bool(getattr(cfg, "MATMON_MODE", False)):
        return None

    ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine is not None else None
    tick_buffer = getattr(ticker, "tick_buffer", None) if ticker is not None else None
    if tick_buffer is None:
        return None

    resolved_window = float(
        window_seconds
        if window_seconds is not None
        else getattr(cfg, "MATMON_QUOTE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)
    )
    return start_capture(symbol, di_passed_at, tick_buffer=tick_buffer, window_seconds=resolved_window)


def pop_frozen_evidence(symbol, di_passed_at, *, wait_timeout=DEFAULT_WAIT_TIMEOUT_SECONDS):
    """Consume the frozen tick batch captured for (symbol, di_passed_at).

    Returns ``None`` -- meaning "no frozen evidence, caller must fall back to
    the live-buffer path" -- if no matching capture was ever started, if it
    was started for a different (stale or newer) di_passed_at, or if it
    fails to become ready within ``wait_timeout``. Never silently skips
    CLEAN; the caller decides what "no evidence" means.
    """
    with _lock:
        entry = _entries.get(symbol)

    if entry is None or entry.di_passed_at != float(di_passed_at):
        return None

    if not entry.ready.wait(timeout=wait_timeout):
        logger.info("MATMON_FREEZE_TIMEOUT | %s | di_passed_at=%s", symbol, di_passed_at)
        return None

    with _lock:
        if _entries.get(symbol) is entry:
            _entries.pop(symbol, None)

    if entry.reason != "CAPTURED":
        logger.info("MATMON_FREEZE_UNAVAILABLE | %s | reason=%s", symbol, entry.reason)
        return None

    return entry.ticks


def reset_for_tests():
    """Test-only helper: clear all in-flight/frozen entries."""
    with _lock:
        _entries.clear()
