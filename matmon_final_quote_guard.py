#!/usr/bin/env python3
"""Final Matmon quote recheck after first-three confirmation.

This guard never creates or reverses a signal. It waits briefly for one quote
newer than the third confirmation tick and fails closed on missing/stale depth,
material spread widening, or reversal beyond the first-three range.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class FinalQuoteGuard:
    accepted: bool
    reason: str
    received_at: float | None = None
    age_seconds: float | None = None
    spread_bps: float | None = None
    baseline_spread_bps: float | None = None
    bid: float | None = None
    ask: float | None = None


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _quote(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if not buys or not sells:
        return None
    try:
        bid = _number(buys[0].get("price"))
        ask = _number(sells[0].get("price"))
    except (AttributeError, IndexError):
        return None
    received_at = _number((tick or {}).get("received_at"))
    if bid is None or ask is None or received_at is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid or received_at <= 0:
        return None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0
    return received_at, bid, ask, spread_bps


def evaluate_final_quote(
    tick_buffer,
    symbol,
    direction,
    clean,
    *,
    timeout_seconds=0.75,
    poll_seconds=0.02,
    max_age_seconds=0.75,
    max_spread_multiplier=1.5,
    spread_allowance_bps=1.0,
    now_fn=time.time,
    sleep_fn=time.sleep,
):
    direction = str(direction or "").upper()
    if direction not in {"BUY", "SELL"}:
        return FinalQuoteGuard(False, "MATMON_FINAL_INVALID_DIRECTION")

    first_bid = _number(getattr(clean, "first_bid", None))
    first_ask = _number(getattr(clean, "first_ask", None))
    last_bid = _number(getattr(clean, "last_bid", None))
    last_ask = _number(getattr(clean, "last_ask", None))
    last_received = _number(getattr(clean, "last_received_at", None))
    if None in (first_bid, first_ask, last_bid, last_ask, last_received):
        return FinalQuoteGuard(False, "MATMON_FINAL_CONFIRMATION_RANGE_UNAVAILABLE")

    baseline_mid = (last_bid + last_ask) / 2.0
    if baseline_mid <= 0 or last_ask < last_bid:
        return FinalQuoteGuard(False, "MATMON_FINAL_CONFIRMATION_QUOTE_INVALID")
    baseline_spread = (last_ask - last_bid) / baseline_mid * 10_000.0

    deadline = now_fn() + max(0.0, float(timeout_seconds))
    latest = None
    while True:
        candidate = _quote(tick_buffer.latest(symbol))
        if candidate is not None and candidate[0] > last_received:
            latest = candidate
            break
        remaining = deadline - now_fn()
        if remaining <= 0:
            break
        sleep_fn(min(float(poll_seconds), remaining))

    if latest is None:
        return FinalQuoteGuard(
            False,
            "MATMON_FINAL_NEW_QUOTE_TIMEOUT",
            baseline_spread_bps=baseline_spread,
        )

    received_at, bid, ask, spread_bps = latest
    age = max(0.0, now_fn() - received_at)
    common = dict(
        received_at=received_at,
        age_seconds=age,
        spread_bps=spread_bps,
        baseline_spread_bps=baseline_spread,
        bid=bid,
        ask=ask,
    )
    if age > float(max_age_seconds):
        return FinalQuoteGuard(False, "MATMON_FINAL_QUOTE_STALE", **common)

    spread_limit = max(
        baseline_spread * float(max_spread_multiplier),
        baseline_spread + float(spread_allowance_bps),
    )
    if spread_bps > spread_limit:
        return FinalQuoteGuard(False, "MATMON_FINAL_SPREAD_WIDENED", **common)

    if direction == "BUY" and (bid < first_bid or ask < first_ask):
        return FinalQuoteGuard(False, "MATMON_FINAL_BUY_REVERSED", **common)
    if direction == "SELL" and (bid > first_bid or ask > first_ask):
        return FinalQuoteGuard(False, "MATMON_FINAL_SELL_REVERSED", **common)

    return FinalQuoteGuard(True, "MATMON_FINAL_QUOTE_CONFIRMED", **common)
