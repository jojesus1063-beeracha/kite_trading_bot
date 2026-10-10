#!/usr/bin/env python3
"""Observation-only Matmon post-DI tick deadline and retry research.

Nothing in this module can authorize, reject, size, route, or exit a trade.
It runs in a daemon thread and writes evidence for later analysis.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import statistics
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from matmon_final_quote_guard import evaluate_final_quote
from matmon_microstructure import evaluate_microstructure
from matmon_quote_confirmation import evaluate_quote_window

logger = logging.getLogger("matmon_tick_observation_shadow")
IST = ZoneInfo("Asia/Kolkata")
DEADLINES = (3.0, 4.0, 5.0, 7.0)
_write_lock = threading.Lock()


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_best_depth(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if not buys or not sells:
        return False
    try:
        bid = _number(buys[0].get("price"))
        ask = _number(sells[0].get("price"))
    except (AttributeError, IndexError):
        return False
    return bid is not None and ask is not None and bid > 0 and ask >= bid


def _has_full_depth(tick):
    depth = (tick or {}).get("depth") or {}
    return len(depth.get("buy") or []) >= 5 and len(depth.get("sell") or []) >= 5


def _timestamp(tick):
    return _number((tick or {}).get("received_at"))


def _ltp(tick):
    value = _number((tick or {}).get("last_price"))
    return value if value is not None else _number((tick or {}).get("ltp"))


def _append(record, root=Path("runtime/matmon/tick_observation")):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{record['session_date']}.jsonl"
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    with _write_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return path


def observe(symbol, direction, di_passed_at, tick_buffer, *,
            deadlines=DEADLINES, sleep_fn=time.sleep, now_fn=time.time,
            output_root=Path("runtime/matmon/tick_observation")):
    """Collect seven seconds of evidence and persist one shadow record."""
    end = float(di_passed_at) + max(deadlines)
    while True:
        remaining = end - now_fn()
        if remaining <= 0:
            break
        sleep_fn(min(0.05, remaining))

    rows = tick_buffer.ticks_received_since(symbol, float(di_passed_at)) or []
    rows = [
        tick for tick in rows
        if (ts := _timestamp(tick)) is not None and float(di_passed_at) <= ts <= end
    ]
    rows.sort(key=_timestamp)
    valid = [tick for tick in rows if _valid_best_depth(tick)]
    full = [tick for tick in valid if _has_full_depth(tick)]
    third_at = _timestamp(valid[2]) if len(valid) >= 3 else None
    completion = None if third_at is None else max(0.0, third_at - float(di_passed_at))
    available_by = {
        str(int(deadline)): bool(completion is not None and completion <= deadline)
        for deadline in deadlines
    }
    recovered_at = next(
        (deadline for deadline in deadlines if deadline > 3.0 and available_by[str(int(deadline))]),
        None,
    ) if not available_by.get("3") else None

    times = [_timestamp(tick) for tick in valid]
    intervals = [b - a for a, b in zip(times, times[1:]) if b >= a]
    ltps = [_ltp(tick) for tick in valid]
    ltp_pairs = [(a, b) for a, b in zip(ltps, ltps[1:]) if a is not None and b is not None]
    unchanged = sum(a == b for a, b in ltp_pairs)

    first_three = tuple(valid[:3])
    clean = evaluate_quote_window(
        tick_buffer, symbol, direction,
        window_seconds=max(deadlines),
        not_before=di_passed_at,
        frozen_ticks=first_three,
    )
    micro = evaluate_microstructure(direction, clean.ticks) if clean.confirmed else None
    final = None
    if micro is not None and micro.accepted:
        final = evaluate_final_quote(
            tick_buffer, symbol, direction, clean,
            timeout_seconds=0.0,
            max_age_seconds=7.5,
            max_spread_multiplier=1.5,
            spread_allowance_bps=1.0,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )

    now_ist = datetime.now(IST)
    record = {
        "event": "MATMON_TICK_DEADLINE_SHADOW",
        "schema_version": 1,
        "observation_only": True,
        "production_unchanged": True,
        "session_date": now_ist.date().isoformat(),
        "recorded_at": now_ist.isoformat(),
        "symbol": symbol,
        "direction": direction,
        "di_passed_at": float(di_passed_at),
        "total_updates_7s": len(rows),
        "valid_depth_updates_7s": len(valid),
        "full_depth_updates_7s": len(full),
        "full_depth_fraction": (len(full) / len(valid)) if valid else 0.0,
        "invalid_depth_updates_7s": len(rows) - len(valid),
        "first_three_completion_seconds": completion,
        "available_by_seconds": available_by,
        "shadow_recovered_after_3s": recovered_at is not None,
        "shadow_recovered_deadline_seconds": recovered_at,
        "median_valid_tick_interval_seconds": statistics.median(intervals) if intervals else None,
        "updates_per_second": len(valid) / max(deadlines),
        "unchanged_ltp_fraction": (unchanged / len(ltp_pairs)) if ltp_pairs else None,
        "shadow_clean_confirmed": bool(clean.confirmed),
        "shadow_clean_reason": clean.reason,
        "shadow_microstructure_accepted": bool(micro and micro.accepted),
        "shadow_microstructure_reason": None if micro is None else micro.reason,
        "shadow_final_quote_accepted": bool(final and final.accepted),
        "shadow_final_quote_reason": None if final is None else final.reason,
    }
    path = _append(record, root=output_root)
    logger.info(
        "MATMON TICK DEADLINE SHADOW | %s | %s | 3s=%s 4s=%s 5s=%s 7s=%s "
        "completion=%s valid=%s full_pct=%.1f clean=%s micro=%s final=%s "
        "recovered_at=%s | OBSERVATION_ONLY=True",
        symbol, direction,
        available_by["3"], available_by["4"], available_by["5"], available_by["7"],
        completion, len(valid), record["full_depth_fraction"] * 100.0,
        record["shadow_clean_confirmed"], record["shadow_microstructure_accepted"],
        record["shadow_final_quote_accepted"], recovered_at,
    )
    return record, path


def maybe_start_observation(symbol, signal, *, ws_engine, cfg, di_passed_at,
                            thread_factory=threading.Thread):
    if signal is None or di_passed_at is None:
        return None
    if str(getattr(signal, "confidence", "")) != "MATMON_EMA_DI":
        return None
    if not bool(getattr(cfg, "MATMON_MODE", False)):
        return None
    ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine is not None else None
    buffer = getattr(ticker, "tick_buffer", None) if ticker is not None else None
    if buffer is None:
        return None
    thread = thread_factory(
        target=observe,
        args=(symbol, str(signal.direction), float(di_passed_at), buffer),
        daemon=True,
        name=f"matmon-tick-shadow-{symbol}",
    )
    thread.start()
    return thread
