#!/usr/bin/env python3
"""Shadow-only fast adverse-movement observer.

This module NEVER places orders and NEVER changes broker protection. It consumes
an already-buffered WebSocket tick and records whether a live position would
have armed/disarmed/confirmed an early adverse exit.

Initial policy (observation only):
- arm at >= 0.60R adverse movement from entry toward the hard stop
- disarm after recovery below 0.50R adverse
- require at least 3 seconds armed and a second fresh observation
- reject ticks older than 2 seconds
- hard-stop/order handling remains entirely in main.py
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_ARM_R = 0.60
DEFAULT_DISARM_R = 0.50
DEFAULT_CONFIRM_SECONDS = 3.0
DEFAULT_MAX_TICK_AGE_SECONDS = 2.0
DEFAULT_MIN_OBSERVATIONS = 2
DEFAULT_LOG_PATH = Path("runtime/fast_adverse_shadow/events.jsonl")


def _finite_positive(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def adverse_r(position: dict, live_price: float) -> Optional[float]:
    """Return movement against the trade as a fraction of original 1R."""
    entry = _finite_positive(position.get("entry"))
    stop = _finite_positive(position.get("stop"))
    price = _finite_positive(live_price)
    direction = str(position.get("direction") or "").upper()
    if entry is None or stop is None or price is None:
        return None
    risk_distance = abs(entry - stop)
    if risk_distance <= 0 or not math.isfinite(risk_distance):
        return None
    if direction == "BUY":
        return (entry - price) / risk_distance
    if direction == "SELL":
        return (price - entry) / risk_distance
    return None


def _append_event(event: dict, log_path: Path | str = DEFAULT_LOG_PATH) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def _event(position: dict, symbol: str, status: str, *, price=None, tick_age=None,
           adverse=None, now_epoch=None, reason=None) -> dict:
    return {
        "timestamp": float(now_epoch if now_epoch is not None else time.time()),
        "symbol": symbol,
        "direction": position.get("direction"),
        "entry": position.get("entry"),
        "stop": position.get("stop"),
        "qty": position.get("qty"),
        "status": status,
        "live_price": price,
        "tick_age_seconds": tick_age,
        "adverse_r": adverse,
        "reason": reason,
        "armed_at": position.get("fast_adverse_shadow_armed_at"),
        "armed_price": position.get("fast_adverse_shadow_armed_price"),
        "observations": position.get("fast_adverse_shadow_observations", 0),
    }


def observe_fast_adverse_shadow(
    position: dict,
    symbol: str,
    tick: Optional[dict],
    *,
    now_epoch: Optional[float] = None,
    arm_r: float = DEFAULT_ARM_R,
    disarm_r: float = DEFAULT_DISARM_R,
    confirm_seconds: float = DEFAULT_CONFIRM_SECONDS,
    max_tick_age_seconds: float = DEFAULT_MAX_TICK_AGE_SECONDS,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    log_path: Path | str = DEFAULT_LOG_PATH,
    persist_events: bool = True,
) -> dict:
    """Observe one position using one latest WS tick; never places an order.

    The returned ``status`` is one of NORMAL, ARMED, DISARMED, WOULD_EXIT,
    SKIP_NO_TICK, SKIP_STALE_TICK, or SKIP_INVALID_POSITION_PRICE.
    """
    now = float(now_epoch if now_epoch is not None else time.time())
    if not 0 <= disarm_r < arm_r:
        raise ValueError("require 0 <= disarm_r < arm_r")
    if confirm_seconds < 0 or max_tick_age_seconds <= 0 or min_observations < 2:
        raise ValueError("invalid confirmation/freshness settings")

    if not tick:
        event = _event(position, symbol, "SKIP_NO_TICK", now_epoch=now,
                       reason="no websocket tick buffered")
        return event

    received_at = tick.get("received_at")
    try:
        tick_age = max(0.0, now - float(received_at))
    except (TypeError, ValueError):
        tick_age = None
    if tick_age is None or tick_age > max_tick_age_seconds:
        event = _event(position, symbol, "SKIP_STALE_TICK", price=tick.get("last_price"),
                       tick_age=tick_age, now_epoch=now,
                       reason="websocket tick is stale or lacks received_at")
        return event

    price = _finite_positive(tick.get("last_price"))
    adv = adverse_r(position, price) if price is not None else None
    if price is None or adv is None:
        event = _event(position, symbol, "SKIP_INVALID_POSITION_PRICE", price=price,
                       tick_age=tick_age, adverse=adv, now_epoch=now,
                       reason="invalid direction/entry/stop/live price")
        return event

    state = str(position.get("fast_adverse_shadow_state") or "NORMAL").upper()
    if state not in {"NORMAL", "ARMED", "WOULD_EXIT"}:
        state = "NORMAL"

    if state == "NORMAL":
        if adv >= arm_r:
            position["fast_adverse_shadow_state"] = "ARMED"
            position["fast_adverse_shadow_armed_at"] = now
            position["fast_adverse_shadow_armed_price"] = price
            position["fast_adverse_shadow_observations"] = 1
            event = _event(position, symbol, "ARMED", price=price, tick_age=tick_age,
                           adverse=adv, now_epoch=now,
                           reason=f"adverse_r >= {arm_r:.2f}")
            if persist_events:
                _append_event(event, log_path)
            return event
        position["fast_adverse_shadow_state"] = "NORMAL"
        return _event(position, symbol, "NORMAL", price=price, tick_age=tick_age,
                      adverse=adv, now_epoch=now)

    if state == "ARMED":
        if adv < disarm_r:
            position["fast_adverse_shadow_state"] = "NORMAL"
            position["fast_adverse_shadow_armed_at"] = None
            position["fast_adverse_shadow_armed_price"] = None
            position["fast_adverse_shadow_observations"] = 0
            event = _event(position, symbol, "DISARMED", price=price, tick_age=tick_age,
                           adverse=adv, now_epoch=now,
                           reason=f"adverse_r recovered below {disarm_r:.2f}")
            if persist_events:
                _append_event(event, log_path)
            return event

        observations = int(position.get("fast_adverse_shadow_observations") or 0) + 1
        position["fast_adverse_shadow_observations"] = observations
        armed_at = position.get("fast_adverse_shadow_armed_at")
        try:
            elapsed = max(0.0, now - float(armed_at))
        except (TypeError, ValueError):
            position["fast_adverse_shadow_armed_at"] = now
            elapsed = 0.0

        if adv >= arm_r and elapsed >= confirm_seconds and observations >= min_observations:
            position["fast_adverse_shadow_state"] = "WOULD_EXIT"
            position["fast_adverse_shadow_confirmed_at"] = now
            position["fast_adverse_shadow_confirmed_price"] = price
            position["fast_adverse_shadow_confirmed_r"] = adv
            event = _event(position, symbol, "WOULD_EXIT", price=price, tick_age=tick_age,
                           adverse=adv, now_epoch=now,
                           reason=(f"sustained >= {arm_r:.2f}R for {elapsed:.2f}s "
                                   f"across {observations} observations"))
            if persist_events:
                _append_event(event, log_path)
            return event

        return _event(position, symbol, "ARMED", price=price, tick_age=tick_age,
                      adverse=adv, now_epoch=now,
                      reason=f"confirmation pending ({elapsed:.2f}s, {observations} observations)")

    # WOULD_EXIT is sticky telemetry for this position. No broker action occurs.
    return _event(position, symbol, "WOULD_EXIT", price=price, tick_age=tick_age,
                  adverse=adv, now_epoch=now, reason="shadow trigger already confirmed")
