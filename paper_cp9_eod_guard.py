#!/usr/bin/env python3
"""PAPER-only CP9 failed-development exit + rest-of-day symbol lock.

Selected research rule:
- evaluate exactly once, at the first position check with age >= 9 minutes;
- if MAE-from-entry <= -0.20% AND current P/L < 0, exit the remaining PAPER position;
- after that CP9 trigger, block any new PAPER entry in that symbol for the rest
  of the same IST trading day.

Safety / scope:
- PAPER only; installation fails if PAPER_TRADING is not true;
- native emergency stop / target / hybrid handling keeps first priority;
- this layer is intended to run before the existing MAE and MFE/time overlays;
- checkpoint evaluation is persisted in open_positions.json through the normal
  position store, so a process restart cannot re-evaluate the same trade later;
- EOD locks are persisted under runtime/paper_risk and survive same-day restarts;
- stale lock files are ignored on a new IST date;
- an unreadable/corrupt same-day lock file fails closed for new PAPER entries;
- no LIVE configuration, broker protection, or live entry path is changed.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd

import config as cfg
import paper_mfe_time_launcher as mfe_time

logger = logging.getLogger("paper_cp9_eod_guard")

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "runtime" / "paper_risk" / "cp9_eod_locks.json"
AUDIT_PATH = ROOT / "runtime" / "paper_audit" / "cp9_eod_audit.jsonl"

DEFAULT_CHECKPOINT_MINUTES = 9.0
DEFAULT_MAE_THRESHOLD_PCT = -0.20


def _ist_now():
    return pd.Timestamp.now(tz="Asia/Kolkata")


def _today_text(now=None):
    ts = pd.Timestamp(now) if now is not None else _ist_now()
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    else:
        ts = ts.tz_convert("Asia/Kolkata")
    return ts.strftime("%Y-%m-%d")


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as h:
        tmp = Path(h.name)
        json.dump(payload, h, indent=2, default=str)
        h.flush()
        os.fsync(h.fileno())
    os.replace(tmp, path)


def _append_audit(payload: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = dict(payload)
    row.setdefault("logged_at", _ist_now().isoformat())
    row.setdefault("paper_only", True)
    with AUDIT_PATH.open("a", encoding="utf-8") as h:
        h.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


def _empty_state(now=None):
    return {"date": _today_text(now), "locks": {}}


def _load_lock_state(now=None, *, fail_closed=True):
    """Return current-day lock state.

    Stale state from another date is treated as empty.  Corrupt/unreadable
    current storage raises when fail_closed=True so the caller can block entry.
    """
    today = _today_text(now)
    if not LOCK_PATH.exists():
        return {"date": today, "locks": {}}
    try:
        raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("root is not an object")
        if raw.get("date") != today:
            return {"date": today, "locks": {}}
        locks = raw.get("locks", {})
        if not isinstance(locks, dict):
            raise ValueError("locks is not an object")
        return {"date": today, "locks": locks}
    except Exception:
        if fail_closed:
            raise
        return {"date": today, "locks": {}}


def _lock_symbol(symbol: str, detail: dict | None = None, now=None) -> dict:
    ts = pd.Timestamp(now) if now is not None else _ist_now()
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    else:
        ts = ts.tz_convert("Asia/Kolkata")
    state = _load_lock_state(ts, fail_closed=True)
    locks = dict(state.get("locks", {}))
    payload = {
        "symbol": symbol,
        "locked_at": ts.isoformat(),
        "reason": "CP9_MAE20_FAILED_DEVELOPMENT_EOD_LOCK",
    }
    if detail:
        payload.update(detail)
    locks[str(symbol)] = payload
    state = {"date": ts.strftime("%Y-%m-%d"), "locks": locks}
    _atomic_json_write(LOCK_PATH, state)
    _append_audit({"event": "CP9_EOD_SYMBOL_LOCK", **payload})
    return payload


def is_symbol_locked(symbol: str, now=None):
    state = _load_lock_state(now, fail_closed=True)
    item = state.get("locks", {}).get(str(symbol))
    return item is not None, item


def cp9_checkpoint_decision(minutes, mae_pct, current_pct, already_evaluated=False):
    """Pure one-shot checkpoint helper used by runtime and unit tests."""
    if already_evaluated:
        return True, False
    if minutes is None:
        return False, False
    checkpoint = float(
        getattr(cfg, "PAPER_CP9_CHECKPOINT_MINUTES", DEFAULT_CHECKPOINT_MINUTES)
    )
    threshold = float(
        getattr(cfg, "PAPER_CP9_MAE_THRESHOLD_PCT", DEFAULT_MAE_THRESHOLD_PCT)
    )
    if float(minutes) < checkpoint:
        return False, False
    trigger = float(mae_pct) <= threshold and float(current_pct) < 0.0
    return True, trigger


def install_cp9_eod_entry_guard(base_module) -> None:
    """Wrap the PAPER symbol guard with a same-day CP9 lock check."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: CP9 EOD entry guard requires PAPER_TRADING=True")
    if not bool(getattr(cfg, "PAPER_CP9_EOD_ENABLED", False)):
        return

    original_guard = base_module._paper_entry_guard
    if getattr(original_guard, "_paper_cp9_eod_wrapped", False):
        return

    def cp9_guard(symbol, now=None, log_path=None):
        ok, detail = original_guard(symbol, now=now, log_path=log_path)
        if not ok:
            return ok, detail
        try:
            locked, lock_detail = is_symbol_locked(symbol, now=now)
        except Exception as exc:
            blocked = {
                "paper_only": True,
                "active": True,
                "decision": "BLOCK",
                "reason": "CP9_EOD_LOCK_STATE_UNREADABLE",
                "error": f"{type(exc).__name__}: {exc}",
            }
            _append_audit({
                "event": "CP9_ENTRY_BLOCK",
                "symbol": symbol,
                **blocked,
            })
            logger.error("%s: CP9 lock state unreadable; blocking PAPER entry: %s", symbol, exc)
            return False, blocked

        if locked:
            blocked = {
                "paper_only": True,
                "active": True,
                "decision": "BLOCK",
                "reason": "CP9_POST_FAILURE_EOD_LOCK",
                "cp9_lock": lock_detail,
            }
            _append_audit({
                "event": "CP9_ENTRY_BLOCK",
                "symbol": symbol,
                "reason": blocked["reason"],
                "cp9_lock": lock_detail,
            })
            return False, blocked

        enriched = dict(detail) if isinstance(detail, dict) else {"detail": str(detail)}
        enriched.update({
            "cp9_eod_guard": True,
            "cp9_eod_locked": False,
        })
        return True, enriched

    cp9_guard._paper_cp9_eod_wrapped = True
    base_module._paper_entry_guard = cp9_guard
    logger.warning(
        "PAPER CP9 EOD ENTRY GUARD ACTIVE: symbols that trigger CP9 MAE20 are blocked for the rest of the IST day"
    )


def _minutes_in_trade(position):
    entry_time = position.get("entry_time")
    if not entry_time:
        return None
    try:
        entry_dt = pd.to_datetime(entry_time)
        now = pd.Timestamp.now(tz=entry_dt.tz) if entry_dt.tz is not None else pd.Timestamp.now()
        return max(0.0, (now - entry_dt).total_seconds() / 60.0)
    except Exception:
        return None


def _execute_cp9_exit(
    trading_main,
    kite,
    symbol,
    exchange_map,
    open_positions,
    risk,
    last_price,
):
    if symbol not in open_positions:
        return None
    position = open_positions[symbol]
    qty = int(position.get("qty") or 0)
    if qty <= 0:
        return None
    exchange = position.get("exchange", exchange_map.get(symbol, "NSE"))
    reason = "cp9_mae20_failed_development_eod"

    exit_result = trading_main.place_exit_order(
        kite,
        symbol,
        position["direction"],
        qty,
        exchange,
        cfg,
        protection_clearance=None,
    )
    confirmed_qty = int(exit_result.get("filled_quantity") or 0)
    if confirmed_qty <= 0:
        logger.warning(
            "%s: CP9 exit not filled | status=%s; will retry while position remains open",
            symbol,
            exit_result.get("status"),
        )
        position["cp9_exit_pending"] = True
        trading_main.save_positions(open_positions)
        return f"EXIT NOT FILLED ({reason})"

    exit_price = exit_result.get("average_price")
    if exit_price is None:
        exit_price = last_price
    exit_price = float(exit_price)

    cost_result = trading_main.net_pnl_for_trade(
        position["direction"],
        confirmed_qty,
        float(position["entry"]),
        exit_price,
    )
    gross_pnl = cost_result["gross_pnl"]
    costs = cost_result["costs"]
    net_pnl = cost_result["net_pnl"]

    risk.record_trade_result(net_pnl)
    analytics = trading_main._trade_analytics_from_position(position)
    analytics.update({
        "cp9_exit_rule": reason,
        "cp9_checkpoint_minutes": position.get("cp9_checkpoint_minutes"),
        "cp9_checkpoint_mfe_pct": position.get("cp9_checkpoint_mfe_pct"),
        "cp9_checkpoint_mae_pct": position.get("cp9_checkpoint_mae_pct"),
        "cp9_checkpoint_current_pct": position.get("cp9_checkpoint_current_pct"),
        "cp9_eod_lock": True,
    })
    trading_main.record_trade(
        symbol,
        position["direction"],
        confirmed_qty,
        float(position["entry"]),
        exit_price,
        net_pnl,
        reason,
        exchange=exchange,
        gross_pnl=gross_pnl,
        costs=costs,
        analytics=analytics,
    )

    remaining = qty - confirmed_qty
    if remaining <= 0:
        del open_positions[symbol]
    else:
        position["qty"] = remaining
        position["cp9_exit_pending"] = True
        position["last_exit_price"] = exit_price
        position["last_exit_pnl"] = net_pnl
    trading_main.save_positions(open_positions)

    _append_audit({
        "event": "CP9_EXIT_FILLED",
        "symbol": symbol,
        "reason": reason,
        "confirmed_qty": confirmed_qty,
        "remaining_qty": max(0, remaining),
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "costs": costs,
        "net_pnl": net_pnl,
    })
    logger.warning(
        "%s: CP9 EXIT FILLED | qty=%s exit=%.2f net=%.2f remaining=%s",
        symbol,
        confirmed_qty,
        exit_price,
        net_pnl,
        max(0, remaining),
    )
    return f"CLOSED ({reason}) | net P&L {net_pnl:.2f}" if remaining <= 0 else f"PARTIAL ({reason})"


def install_cp9_eod_exit_patch(trading_main) -> None:
    """Install one-shot 9m MAE<=-0.20/current<0 exit in PAPER only."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: CP9 EOD exit requires PAPER_TRADING=True")
    if not bool(getattr(cfg, "PAPER_CP9_EOD_ENABLED", False)):
        return

    original_check = trading_main.check_position_exit
    if getattr(original_check, "_paper_cp9_eod_exit_wrapped", False):
        return

    def check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
        # Native emergency stop / target / hybrid retains first priority.
        status = original_check(
            kite,
            symbol,
            tokens,
            exchange_map,
            open_positions,
            risk,
            check_trend=check_trend,
        )
        if symbol not in open_positions:
            return status
        if not str(status).lower().startswith("position open"):
            return status

        position = open_positions[symbol]

        # A previously-triggered CP9 exit that failed/partially filled is retried.
        if bool(position.get("cp9_exit_pending")):
            last_price = float(position.get("cp9_checkpoint_price") or position.get("entry") or 0.0)
            return _execute_cp9_exit(
                trading_main,
                kite,
                symbol,
                exchange_map,
                open_positions,
                risk,
                last_price,
            ) or status

        already_evaluated = bool(position.get("cp9_checkpoint_evaluated"))
        if already_evaluated:
            return status

        minutes = _minutes_in_trade(position)
        checkpoint = float(
            getattr(cfg, "PAPER_CP9_CHECKPOINT_MINUTES", DEFAULT_CHECKPOINT_MINUTES)
        )
        if minutes is None or minutes < checkpoint:
            return status

        token = tokens.get(symbol)
        if token is None:
            return status

        try:
            df = trading_main.fetch_candles(
                kite,
                token,
                "minute",
                lookback_days=1,
                trim_incomplete=False,
            )
            if df is None or df.empty:
                return status

            entry_dt = pd.to_datetime(position.get("entry_time"))
            dates = pd.to_datetime(df["date"])
            df_since = df.loc[dates >= entry_dt]
            if df_since.empty:
                # Include the order minute when the stored entry timestamp has seconds.
                df_since = df.loc[dates >= entry_dt.floor("min")]
            if df_since.empty:
                return status

            last_price = float(df_since.iloc[-1]["close"])
            mfe, mae, current_pct, giveback_pct = mfe_time._excursions(
                position, df_since, last_price
            )
        except Exception as exc:
            logger.warning("%s: CP9 checkpoint observation failed: %s", symbol, exc)
            return status

        evaluated, trigger = cp9_checkpoint_decision(
            minutes,
            mae,
            current_pct,
            already_evaluated=False,
        )
        if not evaluated:
            return status

        # Persist the one-shot decision before any exit attempt. A restart must
        # never convert this into a later re-evaluation with more adverse data.
        position["cp9_checkpoint_evaluated"] = True
        position["cp9_checkpoint_minutes"] = minutes
        position["cp9_checkpoint_mfe_pct"] = mfe
        position["cp9_checkpoint_mae_pct"] = mae
        position["cp9_checkpoint_current_pct"] = current_pct
        position["cp9_checkpoint_giveback_pct"] = giveback_pct
        position["cp9_checkpoint_price"] = last_price
        position["cp9_checkpoint_triggered"] = bool(trigger)
        trading_main.save_positions(open_positions)

        _append_audit({
            "event": "CP9_CHECKPOINT",
            "symbol": symbol,
            "entry_time": position.get("entry_time"),
            "minutes": minutes,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "current_pct": current_pct,
            "giveback_pct": giveback_pct,
            "triggered": bool(trigger),
            "mae_threshold_pct": float(
                getattr(cfg, "PAPER_CP9_MAE_THRESHOLD_PCT", DEFAULT_MAE_THRESHOLD_PCT)
            ),
        })

        if not trigger:
            logger.info(
                "%s: CP9 CHECKPOINT PASS | minutes=%.1f mfe=%.3f%% mae=%.3f%% current=%.3f%%",
                symbol,
                minutes,
                mfe,
                mae,
                current_pct,
            )
            return status

        # Lock immediately on the failed-development decision. This remains true
        # even if an exit attempt is temporarily unfilled and later closes by a
        # native mechanism; the symbol still failed the selected CP9 test.
        try:
            lock_detail = _lock_symbol(
                symbol,
                {
                    "entry_time": position.get("entry_time"),
                    "checkpoint_minutes": minutes,
                    "mfe_pct": mfe,
                    "mae_pct": mae,
                    "current_pct": current_pct,
                },
            )
        except Exception as exc:
            logger.error("%s: cannot persist CP9 EOD lock; retaining position and failing closed: %s", symbol, exc)
            position["cp9_exit_pending"] = False
            position["cp9_lock_error"] = f"{type(exc).__name__}: {exc}"
            trading_main.save_positions(open_positions)
            return "CP9 LOCK PERSISTENCE ERROR — POSITION LEFT TO NATIVE PROTECTION"

        position["cp9_eod_lock"] = lock_detail
        position["cp9_exit_pending"] = True
        trading_main.save_positions(open_positions)

        logger.warning(
            "%s: CP9 MAE20 EXIT TRIGGER | minutes=%.1f mae=%.3f%% mfe=%.3f%% current=%.3f%% -> EOD symbol lock",
            symbol,
            minutes,
            mae,
            mfe,
            current_pct,
        )
        return _execute_cp9_exit(
            trading_main,
            kite,
            symbol,
            exchange_map,
            open_positions,
            risk,
            last_price,
        ) or status

    check_position_exit._paper_cp9_eod_exit_wrapped = True
    trading_main.check_position_exit = check_position_exit
    logger.warning(
        "PAPER CP9 MAE20 EOD EXIT ACTIVE: one-shot >=%.0fm checkpoint; MAE<=%.2f%% AND current<0 -> exit + block symbol rest of day",
        float(getattr(cfg, "PAPER_CP9_CHECKPOINT_MINUTES", DEFAULT_CHECKPOINT_MINUTES)),
        float(getattr(cfg, "PAPER_CP9_MAE_THRESHOLD_PCT", DEFAULT_MAE_THRESHOLD_PCT)),
    )
