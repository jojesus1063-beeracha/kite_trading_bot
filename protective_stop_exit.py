"""Fail-closed coordination between broker stops and market exits."""

from __future__ import annotations

import logging

from protective_stop import verify_protective_stop_active
from protective_stop_store import (
    get_protective_stop,
    request_protective_stop_exit_coordination,
    reserve_protective_stop_cancel_attempt,
    update_protective_stop_exit_coordination,
    update_protective_stop_verification,
)


logger = logging.getLogger("protective_stop_exit")

TERMINAL_CLEARANCE_STATES = {
    "CANCELLED",
    "REJECTED",
    "TRIGGERED",
    "PARTIALLY_TRIGGERED",
}


def _blocked(
    state,
    reason,
    *,
    operation_id=None,
    order_id=None,
    action=None,
    exit_reason=None,
    record=None,
):
    return {
        "success": False,
        "safe_to_submit_exit": False,
        "position_closed_by_stop": False,
        "state": state,
        "reason": reason,
        "operation_id": operation_id,
        "order_id": order_id,
        "exit_action": action,
        "exit_reason": exit_reason,
        "stop_filled_quantity": int(
            (record or {}).get("filled_quantity") or 0
        ),
        "new_stop_filled_quantity": 0,
        "stop_average_price": (record or {}).get("average_price"),
        "remaining_quantity": None,
        "clearance": None,
    }


def _validate_identity(symbol, position, record):
    exchange = str(position.get("exchange") or "NSE")
    operation_id = position.get("protective_stop_operation_id")

    if not operation_id:
        return "position has no durable protective-stop operation ID"

    if record is None:
        return "durable protective-stop record is missing"

    if record.get("operation_id") != operation_id:
        return "protective-stop operation identity mismatch"

    if str(record.get("symbol")) != str(symbol):
        return "protective-stop symbol mismatch"

    if str(record.get("exchange")) != exchange:
        return "protective-stop exchange mismatch"

    if str(record.get("position_direction")) != str(
        position.get("direction")
    ):
        return "protective-stop position direction mismatch"

    if record.get("order_id") is None:
        return "protective-stop broker order ID is unresolved"

    requested = int(record.get("requested_quantity") or 0)
    applied = int(record.get("applied_filled_quantity") or 0)
    current = int(position.get("qty") or 0)

    if requested <= 0 or applied < 0 or current <= 0:
        return "protective-stop quantities are invalid"

    if current + applied != requested:
        return (
            "protective-stop/local quantity mismatch: "
            f"position={current}, applied_stop={applied}, "
            f"stop_requested={requested}"
        )

    return None


def inspect_protective_stop(
    kite,
    *,
    symbol,
    position,
    cfg,
    store_path=None,
):
    """Read current stop history without cancelling or submitting orders."""

    if getattr(cfg, "PAPER_TRADING", True):
        return {
            "success": True,
            "state": "PAPER",
            "active": False,
            "terminal": True,
            "operation_id": None,
            "order_id": None,
            "requested_quantity": int(position.get("qty") or 0),
            "filled_quantity": 0,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": None,
            "confirmation_pending": False,
            "exit_coordination_requested": False,
            "exit_action": None,
            "exit_reason": None,
        }

    operation_id = position.get("protective_stop_operation_id")
    record = (
        get_protective_stop(operation_id, path=store_path)
        if operation_id
        else None
    )
    identity_error = _validate_identity(symbol, position, record)

    if identity_error:
        return _blocked(
            "STOP_IDENTITY_UNRESOLVED",
            identity_error,
            operation_id=operation_id,
            order_id=(record or {}).get("order_id"),
            record=record,
        )

    verification = verify_protective_stop_active(
        kite,
        str(record["order_id"]),
        int(record["requested_quantity"]),
        max_wait_seconds=getattr(
            cfg,
            "PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS",
            15,
        ),
        poll_interval_seconds=getattr(
            cfg,
            "PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS",
            1,
        ),
    )

    update_protective_stop_verification(
        operation_id,
        verification,
        path=store_path,
    )

    record = get_protective_stop(operation_id, path=store_path)

    return {
        "success": True,
        "state": verification.state,
        "status": verification.status,
        "active": verification.active,
        "terminal": verification.terminal,
        "operation_id": operation_id,
        "order_id": str(record["order_id"]),
        "requested_quantity": int(record["requested_quantity"]),
        "filled_quantity": int(verification.filled_quantity),
        "pending_quantity": int(verification.pending_quantity),
        "cancelled_quantity": int(verification.cancelled_quantity),
        "average_price": verification.average_price,
        "confirmation_pending": verification.state
        in {"TIMEOUT", "UNKNOWN"},
        "status_message": verification.status_message,
        "exit_coordination_requested": bool(
            record.get("exit_coordination_requested")
        ),
        "exit_action": record.get("exit_coordination_action"),
        "exit_reason": record.get("exit_coordination_reason"),
        "cancel_api_attempted": bool(
            record.get("cancel_api_attempted")
        ),
        "record": record,
    }


def coordinate_protective_stop_for_exit(
    kite,
    *,
    symbol,
    position,
    cfg,
    exit_action,
    exit_reason,
    exit_quantity=None,
    store_path=None,
):
    """Cancel/reconcile one stop and issue clearance only when terminal.

    Cancellation is at-most-once. A persisted prior attempt is never sent
    again; subsequent calls only inspect broker history.
    """

    action = str(exit_action).upper()
    exchange = str(position.get("exchange") or "NSE")
    position_quantity = int(position.get("qty") or 0)
    requested_exit_quantity = int(
        position_quantity if exit_quantity is None else exit_quantity
    )

    if (
        position_quantity <= 0
        or requested_exit_quantity <= 0
        or requested_exit_quantity > position_quantity
    ):
        return _blocked(
            "EXIT_QUANTITY_INVALID",
            "requested exit quantity is outside the local position",
            action=action,
            exit_reason=exit_reason,
        )

    if getattr(cfg, "PAPER_TRADING", True):
        clearance = {
            "safe_to_submit_exit": True,
            "paper": True,
            "symbol": symbol,
            "exchange": exchange,
            "quantity": requested_exit_quantity,
            "exit_action": action,
            "protective_stop_state": "PAPER",
            "protective_stop_operation_id": None,
            "protective_stop_order_id": None,
        }
        return {
            "success": True,
            "safe_to_submit_exit": True,
            "position_closed_by_stop": False,
            "state": "PAPER",
            "reason": None,
            "operation_id": None,
            "order_id": None,
            "exit_action": action,
            "exit_reason": exit_reason,
            "stop_filled_quantity": 0,
            "new_stop_filled_quantity": 0,
            "stop_average_price": None,
            "remaining_quantity": position_quantity,
            "clearance": clearance,
        }

    operation_id = position.get("protective_stop_operation_id")
    record = (
        get_protective_stop(operation_id, path=store_path)
        if operation_id
        else None
    )
    identity_error = _validate_identity(symbol, position, record)

    if identity_error:
        return _blocked(
            "STOP_IDENTITY_UNRESOLVED",
            identity_error,
            operation_id=operation_id,
            order_id=(record or {}).get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )

    try:
        request_protective_stop_exit_coordination(
            operation_id,
            exit_action=action,
            exit_reason=str(exit_reason),
            position_quantity=position_quantity,
            path=store_path,
        )
    except Exception as exc:
        return _blocked(
            "COORDINATION_PERSISTENCE_ERROR",
            str(exc),
            operation_id=operation_id,
            order_id=record.get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )

    inspection = inspect_protective_stop(
        kite,
        symbol=symbol,
        position=position,
        cfg=cfg,
        store_path=store_path,
    )

    if not inspection.get("success"):
        update_protective_stop_exit_coordination(
            operation_id,
            state=inspection["state"],
            path=store_path,
        )
        inspection.update({
            "exit_action": action,
            "exit_reason": exit_reason,
        })
        return inspection

    if inspection["state"] == "ACTIVE":
        record = get_protective_stop(operation_id, path=store_path)

        if record.get("cancel_api_attempted"):
            update_protective_stop_exit_coordination(
                operation_id,
                state="CANCELLATION_PENDING",
                path=store_path,
            )
            return _blocked(
                "CANCELLATION_PENDING",
                "the single cancellation attempt was already reserved; "
                "broker history is still active",
                operation_id=operation_id,
                order_id=record.get("order_id"),
                action=action,
                exit_reason=exit_reason,
                record=record,
            )

        try:
            reserved = reserve_protective_stop_cancel_attempt(
                operation_id,
                path=store_path,
            )
        except Exception as exc:
            return _blocked(
                "CANCEL_RESERVATION_ERROR",
                str(exc),
                operation_id=operation_id,
                order_id=record.get("order_id"),
                action=action,
                exit_reason=exit_reason,
                record=record,
            )

        if not reserved:
            return _blocked(
                "CANCELLATION_PENDING",
                "protective-stop cancellation was already attempted",
                operation_id=operation_id,
                order_id=record.get("order_id"),
                action=action,
                exit_reason=exit_reason,
                record=record,
            )

        cancel_response = None
        cancel_error = None

        try:
            cancel_response = kite.cancel_order(
                variety=cfg.VARIETY,
                order_id=str(record["order_id"]),
            )
        except Exception as exc:
            cancel_error = str(exc)
            logger.warning(
                "Protective-stop cancellation response uncertain for "
                "%s:%s order=%s: %s",
                exchange,
                symbol,
                record["order_id"],
                exc,
            )

        update_protective_stop_exit_coordination(
            operation_id,
            state=(
                "CANCEL_API_ACCEPTED"
                if cancel_error is None
                else "CANCEL_RESPONSE_UNCERTAIN"
            ),
            cancel_response_order_id=(
                str(cancel_response)
                if cancel_response is not None
                else None
            ),
            cancel_api_error=cancel_error,
            path=store_path,
        )

        inspection = inspect_protective_stop(
            kite,
            symbol=symbol,
            position=position,
            cfg=cfg,
            store_path=store_path,
        )

    state = inspection.get("state")
    record = get_protective_stop(operation_id, path=store_path)

    if state not in TERMINAL_CLEARANCE_STATES:
        update_protective_stop_exit_coordination(
            operation_id,
            state=(
                "CANCELLATION_PENDING"
                if state == "ACTIVE"
                else f"CANCELLATION_{state or 'UNKNOWN'}"
            ),
            path=store_path,
        )
        return _blocked(
            "CANCELLATION_PENDING"
            if state == "ACTIVE"
            else f"CANCELLATION_{state or 'UNKNOWN'}",
            "protective stop is not terminal; market exit is blocked",
            operation_id=operation_id,
            order_id=record.get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )

    total_filled = int(inspection.get("filled_quantity") or 0)
    already_applied = int(
        record.get("applied_filled_quantity") or 0
    )
    newly_filled = total_filled - already_applied

    if newly_filled < 0 or newly_filled > position_quantity:
        update_protective_stop_exit_coordination(
            operation_id,
            state="STOP_FILL_QUANTITY_MISMATCH",
            path=store_path,
        )
        return _blocked(
            "STOP_FILL_QUANTITY_MISMATCH",
            "broker stop fill cannot be safely applied to the local position",
            operation_id=operation_id,
            order_id=record.get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )

    remaining = position_quantity - newly_filled

    if requested_exit_quantity < position_quantity and newly_filled:
        update_protective_stop_exit_coordination(
            operation_id,
            state="PARTIAL_EXIT_STOP_FILL_CONFLICT",
            path=store_path,
        )
        return _blocked(
            "PARTIAL_EXIT_STOP_FILL_CONFLICT",
            "protective stop filled while a partial market exit was being coordinated",
            operation_id=operation_id,
            order_id=record.get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )

    clearance_quantity = (
        remaining
        if requested_exit_quantity == position_quantity
        else requested_exit_quantity
    )

    if clearance_quantity > remaining:
        return _blocked(
            "EXIT_CLEARANCE_QUANTITY_MISMATCH",
            "requested market exit exceeds the position remaining after stop reconciliation",
            operation_id=operation_id,
            order_id=record.get("order_id"),
            action=action,
            exit_reason=exit_reason,
            record=record,
        )
    coordination_state = f"STOP_TERMINAL_{state}"

    update_protective_stop_exit_coordination(
        operation_id,
        state=coordination_state,
        path=store_path,
    )

    clearance = None

    if remaining > 0:
        clearance = {
            "safe_to_submit_exit": True,
            "paper": False,
            "symbol": symbol,
            "exchange": exchange,
            "quantity": clearance_quantity,
            "exit_action": action,
            "protective_stop_state": state,
            "protective_stop_operation_id": operation_id,
            "protective_stop_order_id": str(record["order_id"]),
        }

    return {
        "success": True,
        "safe_to_submit_exit": remaining > 0,
        "position_closed_by_stop": remaining == 0,
        "state": coordination_state,
        "stop_state": state,
        "reason": inspection.get("status_message"),
        "operation_id": operation_id,
        "order_id": str(record["order_id"]),
        "exit_action": action,
        "exit_reason": exit_reason,
        "stop_filled_quantity": total_filled,
        "new_stop_filled_quantity": newly_filled,
        "stop_average_price": inspection.get("average_price"),
        "remaining_quantity": remaining,
        "clearance": clearance,
    }


def valid_exit_clearance(
    clearance,
    *,
    symbol,
    exchange,
    quantity,
    exit_action,
):
    """Return True only for clearance bound to this exact exit order."""

    if not isinstance(clearance, dict):
        return False

    return (
        clearance.get("safe_to_submit_exit") is True
        and str(clearance.get("symbol")) == str(symbol)
        and str(clearance.get("exchange")) == str(exchange)
        and int(clearance.get("quantity") or 0) == int(quantity)
        and str(clearance.get("exit_action")).upper()
        == str(exit_action).upper()
        and (
            clearance.get("paper") is True
            or str(clearance.get("protective_stop_state"))
            in TERMINAL_CLEARANCE_STATES
        )
    )
