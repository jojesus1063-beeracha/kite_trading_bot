"""Single-process rate limiting for broker REST endpoints."""

from __future__ import annotations

import time
from collections import deque


class ApiRateLimiter:
    """Keep calls inside a rolling-window request limit.

    The trading loop is deliberately single-threaded, so a lightweight
    monotonic-clock limiter is sufficient and avoids concurrent broker
    requests changing position or order state underneath the main loop.
    """

    def __init__(
        self,
        max_calls: int,
        period_seconds: float = 1.0,
        *,
        clock=None,
        sleeper=None,
    ):
        if int(max_calls) <= 0:
            raise ValueError("max_calls must be positive")

        if float(period_seconds) <= 0:
            raise ValueError("period_seconds must be positive")

        self.max_calls = int(max_calls)
        self.period_seconds = float(period_seconds)
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._calls = deque()

    def wait(self) -> float:
        """Wait until one request slot is available and reserve it."""

        total_wait = 0.0

        while True:
            now = self._clock()

            while (
                self._calls
                and now - self._calls[0]
                >= self.period_seconds
            ):
                self._calls.popleft()

            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return total_wait

            delay = max(
                0.0,
                self.period_seconds
                - (now - self._calls[0]),
            )

            if delay <= 0:
                continue

            self._sleeper(delay)
            total_wait += delay


# Kite Connect's documented Historical Candle limit is 3 requests/second.
# A small margin prevents boundary jitter from becoming an HTTP 429 response.
HISTORICAL_API_LIMITER = ApiRateLimiter(
    max_calls=3,
    period_seconds=1.05,
)
