"""
Single-threaded scheduling for position checks during entry scans.

This module does not access broker orders, positions, risk state or
files. It only indicates when the existing position-exit function
should run again.

The existing POSITION_CHECK_SECONDS configuration remains unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable


@dataclass
class CooperativeScanMonitor:
    interval_seconds: float
    clock_fn: Callable[[], float] = time.monotonic
    last_check_at: float = field(init=False)
    completed_checks: int = field(
        init=False,
        default=0,
    )

    def __post_init__(self) -> None:
        self.interval_seconds = max(
            1.0,
            float(self.interval_seconds),
        )

        self.last_check_at = float(
            self.clock_fn()
        )

    def seconds_since_check(self) -> float:
        return max(
            0.0,
            float(self.clock_fn())
            - self.last_check_at,
        )

    def due(self) -> bool:
        return (
            self.seconds_since_check()
            >= self.interval_seconds
        )

    def mark_checked(self) -> None:
        self.last_check_at = float(
            self.clock_fn()
        )

        self.completed_checks += 1
