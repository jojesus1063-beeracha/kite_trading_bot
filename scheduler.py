"""
Candle-aligned scheduler for the trading loop.

This module is entirely independent of the trading engine (strategy,
risk, execution). It only knows about time and state transitions --
it has no idea what a "signal" or "position" even is beyond a count.

Enabling this is controlled by cfg.ENABLE_CANDLE_ALIGNED_POLLING; when
False, main.py never imports or touches this module's scheduling
logic, so today's live behavior is completely unaffected.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import logging

logger = logging.getLogger("scheduler")


class SchedulerState(str, Enum):
    WAIT_FOR_CANDLE = "WAIT_FOR_CANDLE"
    FULL_SCAN = "FULL_SCAN"
    POSITION_MONITOR = "POSITION_MONITOR"
    SHUTDOWN = "SHUTDOWN"


def candle_interval_minutes(timeframe: str) -> int:
    """
    Parses a Kite-style interval string ("3minute", "15minute") into
    an integer number of minutes. Defaults to 5 if unrecognized.
    """
    if timeframe.endswith("minute"):
        try:
            return int(timeframe.replace("minute", ""))
        except ValueError:
            pass
    return 5


def last_completed_candle_close(now: datetime, interval_minutes: int) -> datetime:
    """
    Given the current time, returns the timestamp of the most recently
    COMPLETED candle close for the given interval. A candle starting
    at HH:MM and running `interval_minutes` is "completed" the instant
    the clock reaches HH:(MM+interval_minutes).

    Example (5-min): now=09:37:08 -> last completed close is 09:35:00
    (the 09:30-09:35 candle), since the 09:35-09:40 candle is still
    forming.
    """
    minutes_since_midnight = now.hour * 60 + now.minute
    completed_boundary = (minutes_since_midnight // interval_minutes) * interval_minutes
    boundary_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=completed_boundary)
    return boundary_dt


def next_scan_time(now: datetime, interval_minutes: int, buffer_seconds: int) -> datetime:
    """
    Returns the next moment a full scan should run: the NEXT candle
    close (not the last completed one) plus a small buffer to let the
    broker's data settle.
    """
    last_close = last_completed_candle_close(now, interval_minutes)
    next_close = last_close + timedelta(minutes=interval_minutes)
    return next_close + timedelta(seconds=buffer_seconds)


@dataclass
class SchedulerHeartbeat:
    current_time: datetime
    state: SchedulerState
    last_candle: Optional[datetime]
    next_scan: Optional[datetime]
    watchlist_size: int
    open_positions_count: int
    mode: str
    next_position_check: Optional[datetime]

    def format(self) -> str:
        lines = [
            "-" * 40,
            self.current_time.strftime("%H:%M:%S"),
            "",
            "State:",
            self.state.value,
            "",
            "Last Candle:",
            self.last_candle.strftime("%H:%M") if self.last_candle else "N/A",
            "",
            "Next Scan:",
            self.next_scan.strftime("%H:%M:%S") if self.next_scan else "N/A",
            "",
            "Watchlist:",
            str(self.watchlist_size),
            "",
            "Open Positions:",
            str(self.open_positions_count),
            "",
            "Mode:",
            self.mode,
            "",
            "Next Position Check:",
            self.next_position_check.strftime("%H:%M:%S") if self.next_position_check else "N/A",
            "-" * 40,
        ]
        return "\n".join(lines)


@dataclass
class ScanGuard:
    """
    Duplicate-scan prevention. Tracks the candle-close timestamp of the
    last full scan actually performed. Survives across a restart IF
    the caller persists/restores `last_scanned_candle` externally
    (e.g. from a state file) -- this class itself is in-memory only,
    by design, so it stays simple and testable; persistence is the
    caller's responsibility (see main.py wiring).
    """
    last_scanned_candle: Optional[datetime] = None

    def should_scan(self, current_candle_close: datetime) -> bool:
        """
        True if `current_candle_close` has not been scanned yet.
        Also correctly handles the late-scan-recovery case: if the
        scheduler wakes up late and the "current" completed candle has
        moved forward past what we last scanned, this still returns
        True exactly once for the new candle -- never re-scans a candle
        we've already handled, never skips the latest one.
        """
        if self.last_scanned_candle is None:
            return True
        return current_candle_close > self.last_scanned_candle

    def mark_scanned(self, candle_close: datetime):
        self.last_scanned_candle = candle_close
