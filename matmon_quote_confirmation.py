#!/usr/bin/env python3
"""Matmon quote confirmation from the existing MODE_FULL WebSocket TickBuffer."""
from dataclasses import dataclass, asdict
import time


@dataclass
class QuoteEvidence:
    available: bool
    confirmed: bool
    reason: str
    first_received_at: float | None = None
    last_received_at: float | None = None
    first_bid: float | None = None
    first_ask: float | None = None
    last_bid: float | None = None
    last_ask: float | None = None

    def to_dict(self):
        return asdict(self)


def _best_quote(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if not buys or not sells:
        return None
    try:
        bid = float(buys[0]["price"])
        ask = float(sells[0]["price"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def evaluate_quote_window(tick_buffer, symbol, direction, *, window_seconds=3.0,
                          max_age_seconds=2.0, now=None):
    """Require valid first/latest quotes spanning the configured observation window."""
    now = time.time() if now is None else float(now)
    window_seconds = max(0.1, float(window_seconds))
    max_age_seconds = max(0.1, float(max_age_seconds))
    rows = tick_buffer.ticks_received_since(symbol, now - window_seconds - max_age_seconds)
    valid = []
    for tick in rows:
        q = _best_quote(tick)
        if q is None:
            continue
        received = float(tick.get("received_at") or 0.0)
        if received <= 0:
            continue
        valid.append((received, q[0], q[1]))
    if not valid:
        return QuoteEvidence(False, False, "MATMON_NO_TICKS")
    valid.sort(key=lambda x: x[0])
    last = valid[-1]
    if now - last[0] > max_age_seconds:
        return QuoteEvidence(False, False, "MATMON_STALE_QUOTE")
    cutoff = last[0] - window_seconds
    first_candidates = [row for row in valid if row[0] <= cutoff]
    if not first_candidates:
        return QuoteEvidence(False, False, "MATMON_INSUFFICIENT_QUOTE_WINDOW")
    first = first_candidates[-1]
    if direction == "BUY":
        confirmed = last[1] > first[1] and last[2] > first[2]
    elif direction == "SELL":
        confirmed = last[1] < first[1] and last[2] < first[2]
    else:
        confirmed = False
    return QuoteEvidence(
        True, confirmed,
        "MATMON_QUOTE_CONFIRMED" if confirmed else "MATMON_QUOTE_NOT_CONFIRMED",
        first[0], last[0], first[1], first[2], last[1], last[2],
    )
