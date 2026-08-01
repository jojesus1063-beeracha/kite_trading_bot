"""
Stage 1 of the order-execution-safety fix: a pure, read-only verifier
that confirms what actually happened to a submitted order, instead of
trusting kite.place_order()'s return value (which only confirms
SUBMISSION, never execution). This module makes NO live-path changes
by itself -- it is not called anywhere yet. See executor.py/main.py
for the current (unsafe, to-be-fixed-in-later-stages) behavior this
is designed to eventually replace.

Never submits or retries an order. Read-only: only calls
kite.order_history().
"""
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("order_verification")

TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}
# Statuses observed in Kite Connect's real order lifecycle that are
# NOT terminal -- still being processed by the exchange/broker.
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
    """
    Extracts the fields we need from one order_history record,
    defensively -- a malformed record (missing keys, wrong types)
    returns None rather than raising, so the caller can treat it the
    same as a transient API error.
    """
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
    # Never fabricate an execution price: only meaningful when something actually filled.
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
    terminal broker state, or max_wait_seconds elapses. Uses
    clock_fn() (default time.monotonic, immune to system clock
    adjustments) for the deadline, and sleep_fn (default time.sleep)
    between polls -- both injectable so tests run instantly and
    deterministically, never actually sleeping.

    Interpretation is derived from BOTH the latest broker status AND
    the filled/pending/requested quantities together -- a COMPLETE or
    CANCELLED status with filled_quantity < requested_quantity (but
    > 0) is reported as PARTIALLY_FILLED, never as a full fill.

    Never fabricates an execution price, never submits or retries an
    order. An empty history response or persistent API errors resolve
    to UNKNOWN (not success) if no valid record was ever obtained
    before the deadline; a valid-but-still-pending record at the
    deadline resolves to TIMEOUT instead.
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
            # Kite returns order_history in chronological order (oldest
            # first) -- the LAST record is the most recent known state.
            # NOTE: this ordering assumption is based on Kite Connect's
            # documented behavior, not independently re-verified in this
            # stage against a real multi-update order history -- flagged
            # explicitly as an area of residual uncertainty.
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
            # else: still pending/open -- keep polling

        elapsed = clock_fn() - start
        if elapsed >= max_wait_seconds:
            final_status = "TIMEOUT" if last_parsed is not None else "UNKNOWN"
            return _build_result(order_id, expected_quantity, last_parsed, final_status, False,
                                 history_attempts, api_error_count)

        sleep_fn(poll_interval_seconds)
