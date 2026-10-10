from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class QuotePoint:
    received_at: float
    bid: float
    ask: float


@dataclass(frozen=True)
class CleanResult:
    accepted: bool
    reason: str
    samples: int
    coverage_seconds: float
    directional_fraction: float
    bid_change: float
    ask_change: float


def capture_post_di_window(
    ticks: Iterable[dict],
    *,
    di_passed_at: float,
    window_seconds: float = 3.0,
) -> list[QuotePoint]:
    """
    Freeze exactly the quote evidence generated after DI authorization.

    Eligible interval:
        [di_passed_at, di_passed_at + window_seconds]

    This is intentionally independent of how much later ranking/execution
    happens.
    """
    end = float(di_passed_at) + float(window_seconds)

    points: list[QuotePoint] = []

    for tick in ticks:
        try:
            received = float(tick["received_at"])
        except (KeyError, TypeError, ValueError):
            continue

        if received < di_passed_at or received > end:
            continue

        depth = tick.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []

        if not buys or not sells:
            continue

        try:
            bid = float(buys[0]["price"])
            ask = float(sells[0]["price"])
        except (KeyError, TypeError, ValueError):
            continue

        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        points.append(
            QuotePoint(
                received_at=received,
                bid=bid,
                ask=ask,
            )
        )

    points.sort(key=lambda p: p.received_at)
    return points


def current_monotonic_clean(
    points: list[QuotePoint],
    direction: str,
    *,
    required_coverage_seconds: float = 3.0,
) -> CleanResult:
    """
    Replicates the important behaviour of current _full_path_clean().
    """
    if len(points) < 2:
        return CleanResult(
            False, "INSUFFICIENT_SAMPLES",
            len(points), 0.0, 0.0, 0.0, 0.0
        )

    coverage = points[-1].received_at - points[0].received_at

    if coverage < required_coverage_seconds:
        return CleanResult(
            False, "INSUFFICIENT_COVERAGE",
            len(points), coverage, 0.0,
            points[-1].bid - points[0].bid,
            points[-1].ask - points[0].ask,
        )

    direction = direction.upper()

    if direction == "BUY":
        directional = [
            cur.bid >= prev.bid and cur.ask >= prev.ask
            for prev, cur in zip(points, points[1:])
        ]
        endpoint_ok = (
            points[-1].bid > points[0].bid
            and points[-1].ask > points[0].ask
        )

    elif direction == "SELL":
        directional = [
            cur.bid <= prev.bid and cur.ask <= prev.ask
            for prev, cur in zip(points, points[1:])
        ]
        endpoint_ok = (
            points[-1].bid < points[0].bid
            and points[-1].ask < points[0].ask
        )

    else:
        return CleanResult(
            False, "INVALID_DIRECTION",
            len(points), coverage, 0.0, 0.0, 0.0
        )

    fraction = sum(directional) / len(directional)

    accepted = endpoint_ok and all(directional)

    return CleanResult(
        accepted,
        "CURRENT_CLEAN_PASS" if accepted else "CURRENT_CLEAN_REJECT",
        len(points),
        coverage,
        fraction,
        points[-1].bid - points[0].bid,
        points[-1].ask - points[0].ask,
    )


def consistency_clean(
    points: list[QuotePoint],
    direction: str,
    *,
    required_coverage_seconds: float = 3.0,
    minimum_directional_fraction: float = 0.80,
) -> CleanResult:
    """
    Research alternative.

    Requirements:
      * >= 3 seconds coverage
      * >= 2 valid samples
      * net bid AND ask movement agrees with direction
      * at least 80% of transitions do not oppose direction

    One small quote oscillation therefore does not automatically invalidate
    an otherwise directional post-DI window.
    """
    if len(points) < 2:
        return CleanResult(
            False, "INSUFFICIENT_SAMPLES",
            len(points), 0.0, 0.0, 0.0, 0.0
        )

    coverage = points[-1].received_at - points[0].received_at

    if coverage < required_coverage_seconds:
        return CleanResult(
            False, "INSUFFICIENT_COVERAGE",
            len(points), coverage, 0.0,
            points[-1].bid - points[0].bid,
            points[-1].ask - points[0].ask,
        )

    direction = direction.upper()

    if direction == "BUY":
        transitions = [
            cur.bid >= prev.bid and cur.ask >= prev.ask
            for prev, cur in zip(points, points[1:])
        ]

        endpoint_ok = (
            points[-1].bid > points[0].bid
            and points[-1].ask > points[0].ask
        )

    elif direction == "SELL":
        transitions = [
            cur.bid <= prev.bid and cur.ask <= prev.ask
            for prev, cur in zip(points, points[1:])
        ]

        endpoint_ok = (
            points[-1].bid < points[0].bid
            and points[-1].ask < points[0].ask
        )

    else:
        return CleanResult(
            False, "INVALID_DIRECTION",
            len(points), coverage, 0.0, 0.0, 0.0
        )

    fraction = sum(transitions) / len(transitions)

    accepted = (
        endpoint_ok
        and fraction >= minimum_directional_fraction
    )

    return CleanResult(
        accepted,
        "CONSISTENCY_CLEAN_PASS"
        if accepted
        else "CONSISTENCY_CLEAN_REJECT",
        len(points),
        coverage,
        fraction,
        points[-1].bid - points[0].bid,
        points[-1].ask - points[0].ask,
    )
