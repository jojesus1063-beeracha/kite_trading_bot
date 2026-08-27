#!/usr/bin/env python3
"""Fail-closed Matmon quote repricing from the existing ws_ticker TickBuffer."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional
import time


@dataclass
class QuoteEvidence:
    available: bool
    reason: str
    first_timestamp: Optional[str] = None
    first_bid: Optional[float] = None
    first_ask: Optional[float] = None
    last_timestamp: Optional[str] = None
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    sample_count: int = 0

    def to_dict(self):
        return asdict(self)


def _best_quote(tick):
    depth = (tick or {}).get("depth") or {}
    buys, sells = depth.get("buy"), depth.get("sell")
    if not isinstance(buys, list) or not buys or not isinstance(sells, list) or not sells:
        return None
    try:
        bid = float(buys[0]["price"])
        ask = float(sells[0]["price"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def collect_quote_evidence(tick_buffer, symbol, window_seconds=3.0, max_stale_seconds=2.0, now=None):
    """Read an already-populated TickBuffer; never starts a socket or calls REST."""
    now_epoch = time.time() if now is None else float(now)
    latest = tick_buffer.latest(symbol)
    if latest is None:
        return QuoteEvidence(False, "MATMON_NO_TICKS")
    received = latest.get("received_at")
    if received is None or now_epoch - float(received) > max_stale_seconds:
        return QuoteEvidence(False, "MATMON_STALE_QUOTE")

    ts = latest.get("exchange_timestamp")
    if not isinstance(ts, datetime):
        return QuoteEvidence(False, "MATMON_INVALID_DEPTH")
    since = ts - timedelta(seconds=float(window_seconds))
    rows = tick_buffer.ticks_since(symbol, since)
    valid = [(t, _best_quote(t)) for t in rows]
    valid = [(t, q) for t, q in valid if q is not None]
    if len(valid) < 2:
        return QuoteEvidence(False, "MATMON_INSUFFICIENT_QUOTE_WINDOW", sample_count=len(valid))

    first_tick, (first_bid, first_ask) = valid[0]
    last_tick, (last_bid, last_ask) = valid[-1]
    first_ts, last_ts = first_tick["exchange_timestamp"], last_tick["exchange_timestamp"]
    coverage = (last_ts - first_ts).total_seconds()
    if coverage < float(window_seconds) * 0.5:
        return QuoteEvidence(False, "MATMON_INSUFFICIENT_QUOTE_WINDOW", sample_count=len(valid))

    return QuoteEvidence(
        True, "MATMON_QUOTE_EVIDENCE_READY",
        first_ts.isoformat(), first_bid, first_ask,
        last_ts.isoformat(), last_bid, last_ask, len(valid),
    )
