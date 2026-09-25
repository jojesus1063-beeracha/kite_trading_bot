#!/usr/bin/env python3
"""EL-BETHEL V2.1 failure-to-launch observer (shadow only).

This module never places, modifies, or cancels broker orders.
It evaluates an already-open position after a configurable age and records
whether the position appears to have launched, failed to launch, or deserves
extra time because its entry context was exceptionally strong.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH = Path("runtime/failure_to_launch_shadow/events.jsonl")
CHECK_AFTER_MINUTES = 9.0
MFE_WARNING_PCT = 0.10
ADX_STRONG = 30.0
DI_GAP_STRONG = 20.0


def _num(value: Any):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def directional_gap(position: dict) -> float | None:
    ctx = position.get("entry_context_detail") or {}
    plus = _num(ctx.get("plus_di"))
    minus = _num(ctx.get("minus_di"))
    direction = str(position.get("direction") or "").upper()
    if plus is None or minus is None:
        return None
    if direction == "BUY":
        return plus - minus
    if direction == "SELL":
        return minus - plus
    return None


def _append(event: dict, log_path: Path | str) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def observe_failure_to_launch(
    position: dict,
    symbol: str,
    current_price: float,
    *,
    now_epoch: float | None = None,
    check_after_minutes: float = CHECK_AFTER_MINUTES,
    mfe_warning_pct: float = MFE_WARNING_PCT,
    adx_strong: float = ADX_STRONG,
    di_gap_strong: float = DI_GAP_STRONG,
    log_path: Path | str = DEFAULT_LOG_PATH,
    persist_events: bool = True,
) -> dict:
    """Return shadow telemetry only; never request a trade action."""
    if check_after_minutes <= 0:
        raise ValueError("check_after_minutes must be positive")
    if mfe_warning_pct < 0:
        raise ValueError("mfe_warning_pct must be non-negative")

    now = float(now_epoch if now_epoch is not None else time.time())

    if position.get("failure_to_launch_shadow_evaluated"):
        return {"status": "ALREADY_EVALUATED", "symbol": symbol, "SHADOW_ONLY": True}

    entry = _num(position.get("entry"))
    current = _num(current_price)
    direction = str(position.get("direction") or "").upper()
    entry_time = position.get("entry_time")

    if entry is None or entry <= 0 or current is None or current <= 0:
        return {"status": "INVALID", "symbol": symbol, "reason": "invalid entry/current", "SHADOW_ONLY": True}
    if direction not in {"BUY", "SELL"}:
        return {"status": "INVALID", "symbol": symbol, "reason": "invalid direction", "SHADOW_ONLY": True}
    if not entry_time:
        return {"status": "INVALID", "symbol": symbol, "reason": "missing entry_time", "SHADOW_ONLY": True}

    try:
        import pandas as pd
        entry_ts = pd.Timestamp(entry_time)
        if entry_ts.tzinfo is not None:
            now_ts = pd.Timestamp.fromtimestamp(now, tz=entry_ts.tz)
        else:
            now_ts = pd.Timestamp.fromtimestamp(now)
        minutes = (now_ts - entry_ts).total_seconds() / 60.0
    except Exception:
        return {"status": "INVALID", "symbol": symbol, "reason": "entry_time parse failed", "SHADOW_ONLY": True}

    if minutes < check_after_minutes:
        return {"status": "WAITING", "symbol": symbol, "minutes_in_trade": minutes, "SHADOW_ONLY": True}

    mfe = max(_num(position.get("mfe_pct")) or 0.0, 0.0)
    if direction == "BUY":
        current_return = (current - entry) / entry * 100.0
    else:
        current_return = (entry - current) / entry * 100.0

    ctx = position.get("entry_context_detail") or {}
    adx = _num(ctx.get("adx_current"))
    gap = directional_gap(position)
    strong_entry = bool(
        adx is not None and gap is not None
        and adx >= adx_strong and gap >= di_gap_strong
    )

    low_mfe = mfe <= mfe_warning_pct
    no_progress = current_return <= 0.0

    if not low_mfe:
        status = "HEALTHY_LAUNCH"
        reason = f"MFE {mfe:.4f}% exceeded {mfe_warning_pct:.2f}%"
    elif strong_entry:
        status = "STRONG_ENTRY_EXCEPTION"
        reason = "low early MFE but entry strength is exceptional; shadow grants more time"
    elif no_progress:
        status = "FAILURE_TO_LAUNCH_SHADOW"
        reason = (
            f"after {minutes:.1f} min MFE={mfe:.4f}% and "
            f"current directional return={current_return:.4f}%"
        )
    else:
        status = "WAITING"
        reason = "low MFE but position remains directionally positive"

    event = {
        "timestamp": now,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "current_price": current,
        "minutes_in_trade": minutes,
        "mfe_pct": mfe,
        "current_return_pct": current_return,
        "entry_adx": adx,
        "entry_di_gap": gap,
        "strong_entry_exception": strong_entry,
        "status": status,
        "reason": reason,
        "SHADOW_ONLY": True,
    }

    if status in {"HEALTHY_LAUNCH", "FAILURE_TO_LAUNCH_SHADOW", "STRONG_ENTRY_EXCEPTION"}:
        position["failure_to_launch_shadow_evaluated"] = True
        position["failure_to_launch_shadow_status"] = status
        position["failure_to_launch_shadow_at"] = now
        if persist_events:
            _append(event, log_path)

    return event
