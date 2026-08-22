#!/usr/bin/env python3
"""Shadow-only market microstructure analytics from Kite FULL-mode ticks.

This module never places/cancels/modifies orders and never changes trading decisions.
It derives entry-context telemetry from the already-running Kite WebSocket tick buffer.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

EVENTS_PATH = Path("runtime/kite_microstructure_shadow/events.jsonl")
MAX_TICK_AGE_SECONDS = 2.0


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _sum_depth(levels: Iterable[dict]) -> float:
    total = 0.0
    for row in levels or []:
        q = _num((row or {}).get("quantity"))
        if q is not None and q > 0:
            total += q
    return total


def _sum_orders(levels: Iterable[dict]) -> float:
    total = 0.0
    for row in levels or []:
        n = _num((row or {}).get("orders"))
        if n is not None and n > 0:
            total += n
    return total


def _best_price(levels: Iterable[dict]) -> Optional[float]:
    for row in levels or []:
        p = _num((row or {}).get("price"))
        if p is not None and p > 0:
            return p
    return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _tick_age_seconds(tick: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    received = tick.get("received_at")
    if isinstance(received, datetime):
        dt = received
    elif received:
        try:
            dt = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def derive_microstructure(tick: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return derived microstructure metrics from one Kite FULL-mode tick."""
    depth = tick.get("depth") or {}
    buys = list(depth.get("buy") or [])[:5]
    sells = list(depth.get("sell") or [])[:5]

    best_bid = _best_price(buys)
    best_ask = _best_price(sells)
    ltp = _num(tick.get("last_price"))
    bid_qty_5 = _sum_depth(buys)
    ask_qty_5 = _sum_depth(sells)
    bid_orders_5 = _sum_orders(buys)
    ask_orders_5 = _sum_orders(sells)

    depth_total = bid_qty_5 + ask_qty_5
    depth_imbalance = None if depth_total <= 0 else (bid_qty_5 - ask_qty_5) / depth_total

    total_buy = _num(tick.get("total_buy_quantity"))
    total_sell = _num(tick.get("total_sell_quantity"))
    total_book = None
    if total_buy is not None and total_sell is not None and total_buy + total_sell > 0:
        total_book = (total_buy - total_sell) / (total_buy + total_sell)

    spread = None
    spread_pct = None
    midpoint = None
    microprice = None
    if best_bid is not None and best_ask is not None and best_ask >= best_bid:
        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2.0
        if midpoint > 0:
            spread_pct = spread / midpoint * 100.0
        top_bid_q = _num((buys[0] if buys else {}).get("quantity")) or 0.0
        top_ask_q = _num((sells[0] if sells else {}).get("quantity")) or 0.0
        denom = top_bid_q + top_ask_q
        if denom > 0:
            microprice = (best_ask * top_bid_q + best_bid * top_ask_q) / denom

    age = _tick_age_seconds(tick, now=now)
    fresh = age is not None and age <= MAX_TICK_AGE_SECONDS

    pressure = "NEUTRAL"
    if depth_imbalance is not None:
        if depth_imbalance >= 0.20:
            pressure = "BUY"
        elif depth_imbalance <= -0.20:
            pressure = "SELL"

    return {
        "last_price": ltp,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "midpoint": midpoint,
        "microprice": microprice,
        "top5_bid_quantity": bid_qty_5,
        "top5_ask_quantity": ask_qty_5,
        "top5_bid_orders": bid_orders_5,
        "top5_ask_orders": ask_orders_5,
        "depth_imbalance": depth_imbalance,
        "total_buy_quantity": total_buy,
        "total_sell_quantity": total_sell,
        "total_book_imbalance": total_book,
        "pressure": pressure,
        "tick_age_seconds": age,
        "fresh": fresh,
        "exchange_timestamp": _iso(tick.get("exchange_timestamp")),
        "received_at": _iso(tick.get("received_at")),
    }


def observe_candidate_microstructure(symbol: str, direction: str, ws_engine: Any, *, extra: Optional[dict] = None) -> Dict[str, Any]:
    """Read the latest buffered tick and append shadow telemetry.

    Fail-safe behavior: missing engine/ticker/tick returns a SKIP record. No trading
    decision is ever returned from this function.
    """
    event: Dict[str, Any] = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "direction": direction,
        "mode": "SHADOW_ONLY",
    }
    if extra:
        event.update(extra)

    try:
        ticker = getattr(ws_engine, "ws_ticker", None)
        buffer = getattr(ticker, "tick_buffer", None)
        latest = getattr(buffer, "latest", None)
        tick = latest(symbol) if callable(latest) else None
    except Exception as exc:
        event.update({"status": "SKIP", "reason": "TICK_BUFFER_ERROR", "error": str(exc)})
        return _append(event)

    if not tick:
        event.update({"status": "SKIP", "reason": "NO_LATEST_TICK"})
        return _append(event)

    metrics = derive_microstructure(tick)
    event["microstructure"] = metrics
    if not metrics.get("fresh"):
        event.update({"status": "SKIP", "reason": "STALE_TICK"})
        return _append(event)

    direction = str(direction or "").upper()
    pressure = metrics.get("pressure")
    if pressure == "NEUTRAL":
        alignment = "NEUTRAL"
    elif pressure == direction:
        alignment = "SUPPORTIVE"
    else:
        alignment = "OPPOSING"

    event.update({"status": "OBSERVED", "pressure_alignment": alignment})
    return _append(event)


def _append(event: Dict[str, Any]) -> Dict[str, Any]:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str, separators=(",", ":")) + "\n")
    return event
