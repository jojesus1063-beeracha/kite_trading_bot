#!/usr/bin/env python3
"""PAPER-only daily-loss hardening and aggregate open-risk guard.

This module is deliberately installed only by the PAPER launcher.  It does not
modify the live configuration or live order path on disk.

Safety model
------------
1. A same-day daily-loss halt is sticky across process/service restarts.
2. A restart never clears that halt unless the operator explicitly launches
   the process with PAPER_ALLOW_DAILY_HALT_CLEAR=YES.
3. Once the realized daily-loss threshold is reached, no new PAPER entries are
   allowed for the rest of the session (unless the explicit override is used).
4. Existing positions are NOT force-closed by this guard.  Their existing
   emergency-stop / hybrid / MAE / MFE / square-off management remains active.
5. Before every PAPER entry order, estimated aggregate risk is checked:

       realized_loss + existing_open_risk + proposed_trade_risk

   A new entry is rejected when that total is >= the configured daily-loss
   budget.  Exact-budget equality is intentionally blocked (fail-safe boundary).

Open risk is estimated from the strategy/sizing stop geometry, NOT from the
wider executable PAPER emergency stop.  The current PAPER position builder
preserves that sizing stop in ``paper_strategy_stop`` / ``paper_original_stop``.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config as cfg
import executor
import position_store
import risk_manager as rm

logger = logging.getLogger("paper_daily_risk_guard")

IST = ZoneInfo("Asia/Kolkata")
ALLOW_HALT_CLEAR_ENV = "PAPER_ALLOW_DAILY_HALT_CLEAR"
AUDIT_PATH = (
    Path(__file__).resolve().parent
    / "runtime"
    / "paper_audit"
    / "daily_risk_audit.jsonl"
)

_ACTIVE_RISK_MANAGER = None


class OpenRiskUnavailable(RuntimeError):
    """Raised when aggregate open risk cannot be determined safely."""


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _today() -> str:
    return date.today().isoformat()


def _is_daily_loss_reason(reason) -> bool:
    text = str(reason or "").strip().lower()
    return text.startswith("daily loss limit") or text == "daily_loss_limit"


def _audit(event: str, **payload) -> None:
    record = {
        "event": event,
        "logged_at": _now_iso(),
        "paper_only": True,
        **payload,
    }
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    except OSError as exc:
        # Never weaken a safety decision merely because audit persistence failed.
        logger.warning("Could not persist PAPER daily-risk audit: %s", exc)


def _read_json_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot safely read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Cannot safely read {path}: top-level JSON is not an object")
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".paper-risk.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def reconcile_startup_halt(state_path: str | os.PathLike | None = None) -> dict:
    """Retain a same-day daily-loss halt unless explicitly operator-cleared.

    A corrupt same-day state file is fail-closed: the PAPER process should not
    resume trading when it cannot prove whether the prior session was halted.
    """
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: PAPER daily-risk guard requires PAPER_TRADING=True")

    path = Path(state_path or rm.DAY_STATE_PATH)
    try:
        state = _read_json_state(path)
    except RuntimeError as exc:
        _audit("DAILY_RISK_STATE_UNREADABLE", path=str(path), error=str(exc))
        raise SystemExit(f"SAFETY BLOCK: {exc}") from exc

    if state is None or state.get("date") != _today():
        return {"active": False, "retained": False, "cleared": False}

    halted = bool(state.get("halted"))
    reason = state.get("halt_reason", "")
    if not halted or not _is_daily_loss_reason(reason):
        return {"active": False, "retained": False, "cleared": False}

    realized_pnl = float(state.get("realized_pnl", 0.0) or 0.0)
    threshold = float(getattr(cfg, "CAPITAL", 0.0) or 0.0) * float(
        getattr(cfg, "MAX_DAILY_LOSS_PCT", 0.0) or 0.0
    ) / 100.0

    allow_clear = os.environ.get(ALLOW_HALT_CLEAR_ENV, "").strip().upper() == "YES"
    if not allow_clear:
        _audit(
            "DAILY_LOSS_HALT_RETAINED_AFTER_RESTART",
            realized_pnl=realized_pnl,
            daily_loss_threshold=-threshold,
            halt_reason=reason,
            halted_at=state.get("halted_at"),
            decision="BLOCK_NEW_ENTRIES",
        )
        logger.warning(
            "PAPER DAILY-LOSS HALT RETAINED: pnl=%.2f threshold=%.2f reason=%s",
            realized_pnl,
            -threshold,
            reason,
        )
        return {"active": True, "retained": True, "cleared": False}

    # Explicit operator action only.  The environment variable is intentionally
    # loud and exact; anything other than literal YES is treated as NO.
    state["halted"] = False
    state["halt_reason"] = ""
    state["operator_halt_clear_at"] = _now_iso()
    state["operator_halt_clear_from_reason"] = reason
    state["operator_halt_clear_from_pnl"] = realized_pnl
    try:
        _atomic_write_json(path, state)
    except OSError as exc:
        _audit(
            "DAILY_LOSS_HALT_OVERRIDE_WRITE_FAILED",
            path=str(path),
            realized_pnl=realized_pnl,
            error=str(exc),
            decision="FAIL_CLOSED",
        )
        raise SystemExit(
            f"SAFETY BLOCK: explicit daily-halt override could not be persisted: {exc}"
        ) from exc

    _audit(
        "DAILY_LOSS_HALT_OPERATOR_OVERRIDE",
        realized_pnl=realized_pnl,
        daily_loss_threshold=-threshold,
        previous_halt_reason=reason,
        decision="HALT_CLEARED_BY_EXPLICIT_OPERATOR_OVERRIDE",
    )
    logger.critical(
        "PAPER DAILY-LOSS HALT CLEARED BY EXPLICIT OPERATOR OVERRIDE | pnl=%.2f",
        realized_pnl,
    )
    return {"active": False, "retained": False, "cleared": True}


def _state_path() -> Path:
    return Path(rm.DAY_STATE_PATH)


def _persist_daily_halt_metadata(risk, prior_state: dict | None = None) -> None:
    """Add sticky-halt metadata after risk_manager's normal state write."""
    if not getattr(risk, "persist", False):
        return
    if not bool(getattr(risk.day, "halted", False)):
        return
    if not _is_daily_loss_reason(getattr(risk.day, "halt_reason", "")):
        return

    path = _state_path()
    try:
        state = _read_json_state(path) or {}
    except RuntimeError as exc:
        # The underlying _halt() already attempted to persist halted=True.
        # Do not silently resume; surface the metadata failure loudly.
        logger.error("Could not augment daily halt metadata: %s", exc)
        _audit("DAILY_LOSS_HALT_METADATA_READ_FAILED", error=str(exc))
        return

    previous = prior_state if isinstance(prior_state, dict) else {}
    halted_at = (
        state.get("halted_at")
        or previous.get("halted_at")
        or _now_iso()
    )
    threshold = float(risk.max_loss_amount())

    state.update(
        {
            "date": _today(),
            "trades_taken": int(getattr(risk.day, "trades_taken", 0) or 0),
            "realized_pnl": float(getattr(risk.day, "realized_pnl", 0.0) or 0.0),
            "halted": True,
            "halt_reason": getattr(risk.day, "halt_reason", ""),
            "halt_code": "DAILY_LOSS_LIMIT",
            "halted_at": halted_at,
            "halt_pnl": float(
                previous.get(
                    "halt_pnl",
                    getattr(risk.day, "realized_pnl", 0.0) or 0.0,
                )
            ),
            "halt_threshold": float(previous.get("halt_threshold", -threshold)),
        }
    )
    try:
        _atomic_write_json(path, state)
    except OSError as exc:
        logger.error("Could not persist PAPER daily halt metadata: %s", exc)
        _audit("DAILY_LOSS_HALT_METADATA_WRITE_FAILED", error=str(exc))


def _load_open_positions_fail_closed() -> dict:
    path = Path(position_store.POSITIONS_PATH)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenRiskUnavailable(f"cannot read open positions: {exc}") from exc
    if not isinstance(data, dict):
        raise OpenRiskUnavailable("open_positions.json top-level value is not an object")
    if data.get("date") != _today():
        return {}
    positions = data.get("positions", {})
    if not isinstance(positions, dict):
        raise OpenRiskUnavailable("open_positions.json positions value is not an object")
    return positions


def _finite_float(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OpenRiskUnavailable(f"{label} is unavailable") from exc
    if not math.isfinite(number):
        raise OpenRiskUnavailable(f"{label} is non-finite")
    return number


def _position_sizing_stop(position: dict) -> float:
    for key in (
        "paper_strategy_stop",
        "paper_original_stop",
        "hybrid_original_stop",
        "stop",
    ):
        value = position.get(key)
        if value is None:
            continue
        try:
            stop = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stop) and stop > 0:
            return stop
    raise OpenRiskUnavailable("position sizing stop is unavailable")


def estimate_open_risk(open_positions: dict) -> tuple[float, list[dict]]:
    total = 0.0
    detail = []
    for symbol, position in (open_positions or {}).items():
        if not isinstance(position, dict):
            raise OpenRiskUnavailable(f"{symbol}: position is not an object")
        qty = int(position.get("qty") or 0)
        if qty <= 0:
            continue
        entry = _finite_float(position.get("entry"), f"{symbol} entry")
        stop = _position_sizing_stop(position)
        risk = abs(entry - stop) * qty
        if not math.isfinite(risk):
            raise OpenRiskUnavailable(f"{symbol}: calculated open risk is non-finite")
        total += risk
        detail.append(
            {
                "symbol": symbol,
                "qty": qty,
                "entry": entry,
                "sizing_stop": stop,
                "estimated_open_risk": risk,
            }
        )
    return total, detail


def estimate_proposed_risk(direction: str, quantity: int, entry_plan: dict | None) -> dict:
    if quantity <= 0:
        return {
            "entry": None,
            "sizing_stop": None,
            "quantity": int(quantity),
            "proposed_risk": 0.0,
        }
    if not isinstance(entry_plan, dict):
        raise OpenRiskUnavailable("entry_plan is unavailable")

    entry = _finite_float(entry_plan.get("signal_entry_price"), "proposed entry")
    direction = str(direction or "").upper()
    if direction not in {"BUY", "SELL"}:
        raise OpenRiskUnavailable("proposed direction is unavailable")

    # Always validate against the SAME stop that risk.position_size() used
    # to size this quantity (main.py: planned_stop_price = signal.stop_loss,
    # then qty = risk.position_size(signal.entry_price, planned_stop_price)).
    #
    # The previous logic recomputed a DIFFERENT stop here whenever
    # fixed_target_enabled was True -- a flat STOP_LOSS_PERCENT (0.45%)
    # distance from entry, completely independent of the geometric stop
    # actually used for sizing. Confirmed end-to-end against a real
    # rejected trade (VAML, 2026-08-12 09:46:43): quantity=143 was sized
    # for ~Rs10 of risk against a tight geometric stop, but this function
    # then validated it against a reconstructed 0.4498% stop (matching
    # cfg.STOP_LOSS_PERCENT=0.45 to within rounding) instead of that same
    # geometric stop, producing proposed_risk=Rs305.76 -- ~30x the actual
    # sizing risk -- which the Rs250 daily budget then correctly rejected,
    # every time, for every symbol, all day. The guard was never wrong
    # about the budget; it was validating a trade that was never proposed.
    stop = _finite_float(entry_plan.get("signal_stop_price"), "proposed sizing stop")

    proposed = abs(entry - stop) * int(quantity)
    if not math.isfinite(proposed):
        raise OpenRiskUnavailable("proposed risk is non-finite")
    return {
        "entry": entry,
        "sizing_stop": stop,
        "quantity": int(quantity),
        "proposed_risk": proposed,
    }


def aggregate_risk_decision(
    risk,
    open_positions: dict,
    direction: str,
    quantity: int,
    entry_plan: dict | None,
) -> dict:
    """Return a deterministic PAPER aggregate-risk admission decision."""
    realized_pnl = float(getattr(risk.day, "realized_pnl", 0.0) or 0.0)
    realized_loss = max(0.0, -realized_pnl)
    budget = float(risk.max_loss_amount())
    open_risk, positions = estimate_open_risk(open_positions)
    proposed = estimate_proposed_risk(direction, quantity, entry_plan)
    exposure = realized_loss + open_risk + proposed["proposed_risk"]

    # Equality is blocked intentionally: at the exact configured budget there
    # is no allowance for costs, slippage or gap-through-stop execution.
    allowed = exposure < budget
    return {
        "allowed": allowed,
        "decision": "ALLOW" if allowed else "BLOCK",
        "reason": None if allowed else "PAPER_AGGREGATE_DAILY_RISK_BUDGET",
        "realized_pnl": realized_pnl,
        "realized_loss": realized_loss,
        "daily_loss_budget": budget,
        "daily_loss_threshold": -budget,
        "open_risk": open_risk,
        "open_positions": positions,
        **proposed,
        "aggregate_risk_if_entered": exposure,
        "boundary_policy": "BLOCK_WHEN_AGGREGATE_RISK_GTE_BUDGET",
    }


def _entry_rejection(symbol: str, quantity: int, reason: str) -> dict:
    return {
        "success": False,
        "order_id": None,
        "operation_id": None,
        "status": "REJECTED",
        "reason": reason,
        "requested_quantity": int(quantity),
        "filled_quantity": 0,
        "average_price": None,
        "entry_confirmation_pending": False,
        "resolved": True,
    }


def install_paper_daily_risk_guard() -> None:
    """Install sticky-halt + final aggregate-risk checks in this PAPER process."""
    global _ACTIVE_RISK_MANAGER

    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: PAPER daily-risk guard requires PAPER_TRADING=True")

    cls = rm.RiskManager
    if not getattr(cls, "_paper_sticky_daily_halt_installed", False):
        original_init = cls.__init__
        original_halt = cls._halt
        original_record = cls.record_trade_result
        original_can_take = cls.can_take_new_trade

        def patched_init(self, *args, **kwargs):
            global _ACTIVE_RISK_MANAGER
            original_init(self, *args, **kwargs)
            _ACTIVE_RISK_MANAGER = self
            if bool(getattr(self.day, "halted", False)) and _is_daily_loss_reason(
                getattr(self.day, "halt_reason", "")
            ):
                _audit(
                    "DAILY_LOSS_HALT_LOADED_INTO_RISK_MANAGER",
                    realized_pnl=float(getattr(self.day, "realized_pnl", 0.0) or 0.0),
                    daily_loss_threshold=-float(self.max_loss_amount()),
                    decision="BLOCK_NEW_ENTRIES",
                )

        def patched_halt(self, reason: str):
            was_daily_halted = bool(getattr(self.day, "halted", False)) and _is_daily_loss_reason(
                getattr(self.day, "halt_reason", "")
            )
            prior_state = None
            if getattr(self, "persist", False):
                try:
                    prior_state = _read_json_state(_state_path())
                except RuntimeError:
                    prior_state = None

            original_halt(self, reason)

            if _is_daily_loss_reason(reason):
                _persist_daily_halt_metadata(self, prior_state=prior_state)
                if not was_daily_halted:
                    _audit(
                        "DAILY_LOSS_HALT_TRIGGERED",
                        realized_pnl=float(getattr(self.day, "realized_pnl", 0.0) or 0.0),
                        daily_loss_threshold=-float(self.max_loss_amount()),
                        halt_reason=reason,
                        decision="BLOCK_NEW_ENTRIES_FOR_SESSION",
                    )
                    logger.critical(
                        "PAPER DAILY-LOSS HALT TRIGGERED | pnl=%.2f threshold=%.2f",
                        float(getattr(self.day, "realized_pnl", 0.0) or 0.0),
                        -float(self.max_loss_amount()),
                    )

        def patched_record(self, pnl: float):
            original_record(self, pnl)
            # risk_manager.record_trade_result performs a second normal save
            # after _halt(); restore sticky metadata after that write.
            if bool(getattr(self.day, "halted", False)) and _is_daily_loss_reason(
                getattr(self.day, "halt_reason", "")
            ):
                _persist_daily_halt_metadata(self)

        def patched_can_take(self, current_open_count: int = 0) -> bool:
            allowed = original_can_take(self, current_open_count=current_open_count)
            if not allowed and bool(getattr(self.day, "halted", False)) and _is_daily_loss_reason(
                getattr(self.day, "halt_reason", "")
            ):
                _audit(
                    "ENTRY_REJECTED_DAILY_LOSS_HALT",
                    realized_pnl=float(getattr(self.day, "realized_pnl", 0.0) or 0.0),
                    daily_loss_threshold=-float(self.max_loss_amount()),
                    open_position_count=int(current_open_count),
                    decision="BLOCK",
                    reason="DAILY_LOSS_LIMIT",
                )
            return allowed

        cls.__init__ = patched_init
        cls._halt = patched_halt
        cls.record_trade_result = patched_record
        cls.can_take_new_trade = patched_can_take
        cls._paper_sticky_daily_halt_installed = True

    original_entry = executor.place_entry_order
    if not getattr(original_entry, "_paper_aggregate_risk_guard_wrapped", False):
        def guarded_place_entry_order(
            kite,
            symbol: str,
            direction: str,
            quantity: int,
            exchange: str,
            cfg_obj,
            entry_plan=None,
        ):
            # This wrapper is installed only in a PAPER process.  Keep an
            # additional defensive branch so accidental reuse cannot alter LIVE.
            if not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
                return original_entry(
                    kite,
                    symbol,
                    direction,
                    quantity,
                    exchange,
                    cfg_obj,
                    entry_plan=entry_plan,
                )

            risk = _ACTIVE_RISK_MANAGER
            if risk is None:
                _audit(
                    "ENTRY_REJECTED_RISK_MANAGER_UNAVAILABLE",
                    symbol=symbol,
                    quantity=int(quantity),
                    decision="BLOCK",
                    reason="PAPER_RISK_MANAGER_UNAVAILABLE",
                )
                return _entry_rejection(symbol, quantity, "PAPER_RISK_MANAGER_UNAVAILABLE")

            if bool(getattr(risk.day, "halted", False)) and _is_daily_loss_reason(
                getattr(risk.day, "halt_reason", "")
            ):
                _audit(
                    "ENTRY_REJECTED_DAILY_LOSS_HALT_FINAL_GATE",
                    symbol=symbol,
                    quantity=int(quantity),
                    realized_pnl=float(getattr(risk.day, "realized_pnl", 0.0) or 0.0),
                    daily_loss_threshold=-float(risk.max_loss_amount()),
                    decision="BLOCK",
                    reason="DAILY_LOSS_LIMIT",
                )
                return _entry_rejection(symbol, quantity, "DAILY_LOSS_LIMIT")

            try:
                open_positions = _load_open_positions_fail_closed()
                decision = aggregate_risk_decision(
                    risk,
                    open_positions,
                    direction,
                    int(quantity),
                    entry_plan,
                )
            except (OpenRiskUnavailable, ValueError, TypeError) as exc:
                _audit(
                    "ENTRY_REJECTED_OPEN_RISK_UNAVAILABLE",
                    symbol=symbol,
                    quantity=int(quantity),
                    realized_pnl=float(getattr(risk.day, "realized_pnl", 0.0) or 0.0),
                    daily_loss_threshold=-float(risk.max_loss_amount()),
                    decision="BLOCK",
                    reason="PAPER_OPEN_RISK_UNAVAILABLE",
                    error=str(exc),
                )
                logger.error("%s: PAPER aggregate risk unavailable; entry blocked: %s", symbol, exc)
                return _entry_rejection(symbol, quantity, "PAPER_OPEN_RISK_UNAVAILABLE")

            if not decision["allowed"]:
                _audit(
                    "ENTRY_REJECTED_AGGREGATE_OPEN_RISK",
                    symbol=symbol,
                    direction=direction,
                    exchange=exchange,
                    **decision,
                )
                logger.warning(
                    "%s: PAPER aggregate-risk entry block | realized_loss=%.2f open_risk=%.2f "
                    "proposed=%.2f total=%.2f budget=%.2f",
                    symbol,
                    decision["realized_loss"],
                    decision["open_risk"],
                    decision["proposed_risk"],
                    decision["aggregate_risk_if_entered"],
                    decision["daily_loss_budget"],
                )
                return _entry_rejection(symbol, quantity, decision["reason"])

            return original_entry(
                kite,
                symbol,
                direction,
                quantity,
                exchange,
                cfg_obj,
                entry_plan=entry_plan,
            )

        guarded_place_entry_order._paper_aggregate_risk_guard_wrapped = True
        executor.place_entry_order = guarded_place_entry_order

    logger.warning(
        "PAPER DAILY RISK GUARD ACTIVE: sticky daily-loss halt; restart auto-clear=OFF; "
        "aggregate realized+open+proposed risk must stay strictly below daily-loss budget"
    )
