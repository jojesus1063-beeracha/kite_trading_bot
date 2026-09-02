#!/usr/bin/env python3
"""Durable PAPER-only entry-count and post-loss cooldown guard.

Why this exists separately from RiskManager:
RiskManager.day.trades_taken is incremented by record_trade_result(), so hybrid
partial exits can count more than once for a single entry. This paper experiment
needs limits on actual entries, not exit legs. The guard therefore records each
successful PAPER entry exactly once in its own daily state file.

Live execution is never patched: installation fails closed unless
PAPER_TRADING=True.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

import config as cfg

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "runtime" / "paper_risk" / "entry_state.json"
TRADE_HISTORY_PATH = BASE_DIR / "trade_history.jsonl"


def _now_ist(now=None):
    ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata")
    if ts.tzinfo is None:
        return ts.tz_localize("Asia/Kolkata")
    return ts.tz_convert("Asia/Kolkata")


def _load_state(now=None, state_path=None):
    now = _now_ist(now)
    path = Path(state_path) if state_path is not None else STATE_PATH
    today = now.strftime("%Y-%m-%d")
    if not path.exists():
        return {"date": today, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": today, "entries": []}
    if not isinstance(data, dict) or data.get("date") != today:
        return {"date": today, "entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"date": today, "entries": entries}


def _save_state(state, state_path=None):
    path = Path(state_path) if state_path is not None else STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _trade_group_key(record):
    signal_id = record.get("signal_id")
    if signal_id:
        return f"signal:{signal_id}"
    return "fallback:{symbol}|{direction}|{entry}|{entry_time}".format(
        symbol=record.get("symbol"),
        direction=record.get("direction"),
        entry=record.get("entry"),
        entry_time=record.get("entry_time"),
    )


def _latest_completed_symbol_trade(symbol, now=None, trade_history_path=None):
    """Aggregate hybrid exit legs and return the latest completed trade group."""
    now = _now_ist(now)
    today = now.strftime("%Y-%m-%d")
    path = Path(trade_history_path) if trade_history_path is not None else TRADE_HISTORY_PATH
    groups = {}

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if record.get("date") != today or record.get("symbol") != symbol:
                    continue

                key = _trade_group_key(record)
                group = groups.setdefault(
                    key,
                    {"pnl": 0.0, "last_exit": None, "rows": 0},
                )
                group["pnl"] += float(record.get("pnl") or 0.0)
                group["rows"] += 1

                time_text = record.get("time")
                if time_text:
                    try:
                        exit_ts = pd.Timestamp(f"{today} {time_text}", tz="Asia/Kolkata")
                    except Exception:
                        exit_ts = None
                    if exit_ts is not None and (
                        group["last_exit"] is None or exit_ts > group["last_exit"]
                    ):
                        group["last_exit"] = exit_ts
    except OSError:
        return None

    completed = [g for g in groups.values() if g.get("last_exit") is not None]
    if not completed:
        return None
    return max(completed, key=lambda g: g["last_exit"])


def can_enter(
    symbol,
    *,
    now=None,
    state_path=None,
    trade_history_path=None,
):
    """Return (allowed, detail) for the current PAPER entry request."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        return False, {
            "paper_only": True,
            "decision": "BLOCK",
            "reason": "PAPER_GUARD_USED_OUTSIDE_PAPER_MODE",
        }

    now = _now_ist(now)
    state = _load_state(now=now, state_path=state_path)
    entries = state["entries"]

    daily_limit = int(getattr(cfg, "PAPER_MAX_ENTRIES_PER_DAY", 30))
    symbol_limit = int(getattr(cfg, "PAPER_MAX_TRADES_PER_SYMBOL", 2))
    cooldown = float(getattr(cfg, "PAPER_LOSS_REENTRY_COOLDOWN_MINUTES", 30.0))

    symbol_entries = [e for e in entries if e.get("symbol") == symbol]
    detail = {
        "paper_only": True,
        "daily_entries": len(entries),
        "daily_entry_limit": daily_limit,
        "symbol_entries": len(symbol_entries),
        "symbol_entry_limit": symbol_limit,
        "loss_cooldown_minutes": cooldown,
    }

    if daily_limit > 0 and len(entries) >= daily_limit:
        detail.update({"decision": "BLOCK", "reason": "MAX_PAPER_ENTRIES_PER_DAY"})
        return False, detail

    if symbol_limit > 0 and len(symbol_entries) >= symbol_limit:
        detail.update({"decision": "BLOCK", "reason": "MAX_TRADES_PER_SYMBOL"})
        return False, detail

    latest = _latest_completed_symbol_trade(
        symbol,
        now=now,
        trade_history_path=trade_history_path,
    )
    if latest is not None:
        latest_pnl = float(latest.get("pnl") or 0.0)
        elapsed = max(0.0, (now - latest["last_exit"]).total_seconds() / 60.0)
        detail.update({
            "latest_completed_trade_pnl": latest_pnl,
            "minutes_since_latest_exit": elapsed,
        })
        if latest_pnl < 0 and elapsed < cooldown:
            detail.update({
                "decision": "BLOCK",
                "reason": "LOSS_REENTRY_COOLDOWN",
                "cooldown_remaining_minutes": cooldown - elapsed,
            })
            return False, detail

    detail["decision"] = "ALLOW"
    return True, detail


def record_successful_entry(
    symbol,
    direction,
    quantity,
    *,
    now=None,
    state_path=None,
):
    """Durably record one confirmed PAPER entry exactly once."""
    now = _now_ist(now)
    state = _load_state(now=now, state_path=state_path)
    state["entries"].append({
        "symbol": str(symbol),
        "direction": str(direction),
        "quantity": int(quantity),
        "entered_at": now.isoformat(),
    })
    _save_state(state, state_path=state_path)
    return state


def install_executor_guard():
    """Patch executor.place_entry_order before main imports it by name."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper entry execution guard requires PAPER_TRADING=True"
        )

    import executor

    original = executor.place_entry_order
    if getattr(original, "_paper_entry_guard_wrapped", False):
        return

    def guarded_place_entry_order(
        kite,
        symbol,
        direction,
        quantity,
        exchange,
        cfg_obj,
        entry_plan=None,
    ):
        allowed, detail = can_enter(symbol)
        if not allowed:
            return {
                "success": False,
                "order_id": None,
                "operation_id": None,
                "status": "PAPER_ENTRY_GUARD_BLOCKED",
                "reason": detail.get("reason"),
                "requested_quantity": int(quantity),
                "filled_quantity": 0,
                "average_price": None,
                "entry_confirmation_pending": False,
                "resolved": True,
            }

        result = original(
            kite,
            symbol,
            direction,
            quantity,
            exchange,
            cfg_obj,
            entry_plan=entry_plan,
        )

        if result.get("success") and int(result.get("filled_quantity") or 0) > 0:
            record_successful_entry(
                symbol,
                direction,
                int(result["filled_quantity"]),
            )
        return result

    guarded_place_entry_order._paper_entry_guard_wrapped = True
    executor.place_entry_order = guarded_place_entry_order
