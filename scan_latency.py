"""Helpers for bounded entry scans and execution-latency analytics."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from scheduler import candle_interval_minutes


def select_scan_universe(
    symbols,
    open_position_symbols,
    shortlist_size,
):
    """Select the daily auto-watchlist leaders plus every open position.

    The auto-watchlist is already ordered by its live priority and
    momentum score.  Limiting detailed historical-data evaluation to
    the first N entries therefore retains the highest-priority names
    while keeping all open positions in the monitoring universe.
    """

    unique_symbols = list(
        dict.fromkeys(symbols)
    )
    limit = int(shortlist_size or 0)

    if limit <= 0:
        shortlisted = unique_symbols
    else:
        shortlisted = unique_symbols[:limit]

    excluded = [
        symbol
        for symbol in unique_symbols
        if symbol not in shortlisted
    ]
    scan_universe = list(shortlisted)

    for symbol in open_position_symbols:
        if symbol not in scan_universe:
            scan_universe.append(symbol)

    return shortlisted, scan_universe, excluded


def _in_signal_timezone(value, signal_timestamp):
    timestamp = pd.Timestamp(value)
    signal = pd.Timestamp(signal_timestamp)

    if signal.tzinfo is not None and timestamp.tzinfo is None:
        return timestamp.tz_localize(signal.tzinfo)

    if signal.tzinfo is None and timestamp.tzinfo is not None:
        return timestamp.tz_localize(None)

    if (
        signal.tzinfo is not None
        and timestamp.tzinfo is not None
    ):
        return timestamp.tz_convert(signal.tzinfo)

    return timestamp


def build_entry_timing(
    signal_timestamp,
    entry_timeframe,
    *,
    scan_started_at=None,
    order_submitted_at=None,
):
    """Return exact candle-close-to-order timing for durable analytics."""

    signal_start = pd.Timestamp(
        signal_timestamp
    )
    signal_close = signal_start + pd.Timedelta(
        minutes=candle_interval_minutes(
            entry_timeframe
        )
    )
    scan_start = _in_signal_timezone(
        scan_started_at or datetime.now(),
        signal_start,
    )
    order_submit = _in_signal_timezone(
        order_submitted_at or datetime.now(),
        signal_start,
    )
    delay_seconds = max(
        0.0,
        (
            order_submit - signal_close
        ).total_seconds(),
    )

    return {
        "signal_candle_start": signal_start.isoformat(),
        "signal_candle_close": signal_close.isoformat(),
        "scan_started_at": scan_start.isoformat(),
        "order_submitted_at": order_submit.isoformat(),
        "entry_delay_seconds": round(
            delay_seconds,
            3,
        ),
    }
