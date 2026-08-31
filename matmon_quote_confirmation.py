#!/usr/bin/env python3
"""Matmon full-path CLEAN quote confirmation using MODE_FULL WebSocket ticks."""
from dataclasses import dataclass, asdict
from math import isfinite
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
    ticks: tuple = ()

    def to_dict(self):
        data = asdict(self)
        data["ticks"] = list(self.ticks)
        return data


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _best_quote(tick):
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
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def _full_path_clean(rows, direction):
    if len(rows) < 2 or direction not in {"BUY", "SELL"}:
        return False
    first = rows[0]
    last = rows[-1]

    if direction == "BUY":
        if not (last[1] > first[1] and last[2] > first[2]):
            return False
        return all(
            cur[1] >= prev[1] and cur[2] >= prev[2]
            for prev, cur in zip(rows, rows[1:])
        )

    if not (last[1] < first[1] and last[2] < first[2]):
        return False
    return all(
        cur[1] <= prev[1] and cur[2] <= prev[2]
        for prev, cur in zip(rows, rows[1:])
    )


def evaluate_quote_window(tick_buffer, symbol, direction, *, window_seconds=3.0,
                          max_age_seconds=2.0, now=None):
    """Fail closed unless a fresh full observation window is CLEAN end-to-end."""
    now = time.time() if now is None else float(now)
    window_seconds = max(0.1, float(window_seconds))
    max_age_seconds = max(0.1, float(max_age_seconds))
    rows = tick_buffer.ticks_received_since(symbol, now - window_seconds - max_age_seconds)

    valid = []
    for tick in rows:
        quote = _best_quote(tick)
        received = _number((tick or {}).get("received_at"))
        if quote is None or received is None or received <= 0:
            continue
        valid.append((received, quote[0], quote[1], tick))

    if not valid:
        return QuoteEvidence(False, False, "MATMON_NO_TICKS")

    valid.sort(key=lambda row: row[0])
    last = valid[-1]
    if now - last[0] > max_age_seconds:
        return QuoteEvidence(False, False, "MATMON_STALE_QUOTE")

    cutoff = last[0] - window_seconds
    first_indexes = [i for i, row in enumerate(valid) if row[0] <= cutoff]
    if not first_indexes:
        return QuoteEvidence(False, False, "MATMON_INSUFFICIENT_QUOTE_WINDOW")

    start = first_indexes[-1]
    window = valid[start:]
    if len(window) < 2:
        return QuoteEvidence(False, False, "MATMON_INSUFFICIENT_QUOTE_WINDOW")

    clean_rows = [(row[0], row[1], row[2]) for row in window]
    confirmed = _full_path_clean(clean_rows, direction)
    first = window[0]
    last = window[-1]
    return QuoteEvidence(
        True,
        confirmed,
        "MATMON_QUOTE_CONFIRMED" if confirmed else "MATMON_QUOTE_NOT_CLEAN",
        first[0], last[0], first[1], first[2], last[1], last[2],
        tuple(row[3] for row in window),
    )
