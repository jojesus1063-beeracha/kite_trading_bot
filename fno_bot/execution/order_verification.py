"""
Read-only verifier that confirms what actually happened to a submitted
order via kite.order_history(), instead of trusting kite.place_order()'s
return value (which only confirms SUBMISSION, never execution).

Identical logic to the equity bot's order_verification.py -- this
module is genuinely broker-generic (order_id/quantity in, fill status
out) with zero equity-specific assumptions, so it's duplicated
verbatim rather than reinvented. Never submits or retries an order.
"""
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("fno.order_verification")

TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}
NON_TERMINAL_STATUSES = {"OPEN", "OPEN PENDING", "VALIDATION PENDING",
                          "PUT ORDER REQ RECEIVED", "TRIGGER PENDING", "MODIFY PENDING"}


@dataclass(frozen=True)
class OrderExecutionResult:
    order_id: str
    status: str  # COMPLETE / REJECTED / CANCELLED / PARTIALLY_FILLED / PENDING / TIMEOUT / UNKNOWN
    requested_quantity: int
    filled_quantity: int
    pending_quantity: int
    cancelled_quantity: int
    average_price: Optional[float]
    status_message: Optional[str]
    exchange_order_id: Optional[str]
    terminal: bool
    verified_at: datetime
    history_attempts: int
    api_error_count: int


def _parse_record(record, expected_quantity):
    try:
        status = str(record.get("status", "")).upper()
        filled_qty = int(record.get("filled_quantity") or 0)
        pending_qty = int(record.get("pending_quantity") or 0)
        cancelled_qty = int(record.get("cancelled_quantity") or 0)
        avg_price = record.get("average_price")
        status_message = record.get("status_message")
        exchange_order_id = record.get("exchange_order_id")
        return {
            "status": status, "filled_qty": filled_qty, "pending_qty": pending_qty,
            "cancelled_qty": cancelled_qty, "avg_price": avg_price,
            "status_message": status_message, "exchange_order_id": exchange_order_id,
        }
    except (TypeError, ValueError, AttributeError) as e:
        logger.warning(f"Malformed order_history record, treating as unreadable: {e}")
        return None


def _build_result(order_id, expected_quantity, parsed, norm_status, terminal,
                   history_attempts, api_error_count):
    filled_qty = parsed["filled_qty"] if parsed else 0
    pending_qty = parsed["pending_qty"] if parsed else expected_quantity
    cancelled_qty = parsed["cancelled_qty"] if parsed else 0
    avg_price = parsed["avg_price"] if parsed else None
    avg_price_result = float(avg_price) if (avg_price is not None and filled_qty > 0) else None
    return OrderExecutionResult(
        order_id=order_id, status=norm_status, requested_quantity=expected_quantity,
        filled_quantity=filled_qty, pending_quantity=pending_qty, cancelled_quantity=cancelled_qty,
        average_price=avg_price_result,
        status_message=parsed["status_message"] if parsed else None,
        exchange_order_id=parsed["exchange_order_id"] if parsed else None,
        terminal=terminal, verified_at=datetime.now(),
        history_attempts=history_attempts, api_error_count=api_error_count,
    )


def verify_order_execution(kite, order_id, expected_quantity, max_wait_seconds=15,
                            poll_interval_seconds=1, sleep_fn=None, clock_fn=None):
    """
    Polls kite.order_history(order_id) until the order reaches a
    terminal broker state, or max_wait_seconds elapses. Never
    fabricates an execution price, never submits or retries an order.
    """
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic

    start = clock_fn()
    history_attempts = 0
    api_error_count = 0
    last_parsed = None

    while True:
        history_attempts += 1
        try:
            records = kite.order_history(order_id)
        except Exception as e:
            api_error_count += 1
            records = None
            logger.warning(f"order_history({order_id}) raised (attempt {history_attempts}): {e}")

        if records:
            parsed = _parse_record(records[-1], expected_quantity)
            if parsed is not None:
                last_parsed = parsed
            else:
                api_error_count += 1

        if last_parsed is not None:
            status = last_parsed["status"]
            filled_qty = last_parsed["filled_qty"]

            if status == "COMPLETE":
                norm_status = "PARTIALLY_FILLED" if (0 < filled_qty < expected_quantity) else "COMPLETE"
                return _build_result(order_id, expected_quantity, last_parsed, norm_status, True,
                                     history_attempts, api_error_count)
            if status == "REJECTED":
                return _build_result(order_id, expected_quantity, last_parsed, "REJECTED", True,
                                     history_attempts, api_error_count)
            if status == "CANCELLED":
                norm_status = "PARTIALLY_FILLED" if filled_qty > 0 else "CANCELLED"
                return _build_result(order_id, expected_quantity, last_parsed, norm_status, True,
                                     history_attempts, api_error_count)

        elapsed = clock_fn() - start
        if elapsed >= max_wait_seconds:
            final_status = "TIMEOUT" if last_parsed is not None else "UNKNOWN"
            return _build_result(order_id, expected_quantity, last_parsed, final_status, False,
                                 history_attempts, api_error_count)

        sleep_fn(poll_interval_seconds)
