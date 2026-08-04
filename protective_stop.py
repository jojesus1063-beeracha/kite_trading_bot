"""Live-only Zerodha broker-side protective SL-M engine."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from protective_stop_store import (
    attach_protective_stop_order_id,
    create_protective_stop_intent,
    update_protective_stop_verification,
)


logger = logging.getLogger("protective_stop")

ACTIVE_STATUS = "TRIGGER PENDING"
TERMINAL_STATUSES = {
    "COMPLETE",
    "REJECTED",
    "CANCELLED",
}


class ProtectiveStopError(Exception):
    pass


@dataclass(frozen=True)
class ProtectiveStopVerificationResult:
    order_id: str
    status: str
    state: str
    requested_quantity: int
    filled_quantity: int
    pending_quantity: int
    cancelled_quantity: int
    average_price: Optional[float]
    status_message: Optional[str]
    exchange_order_id: Optional[str]
    active: bool
    terminal: bool
    verified_at: datetime
    history_attempts: int
    api_error_count: int


def make_protective_stop_tag() -> str:
    """Generate a unique Kite-compatible client tag."""

    tag = "KBS" + uuid.uuid4().hex[:17]

    if len(tag) != 20 or not tag.isalnum():
        raise ProtectiveStopError(
            "Generated protective-stop tag is invalid"
        )

    return tag


def reconcile_protective_stop_submission(
    kite,
    *,
    client_tag,
    symbol,
    exchange,
    stop_side,
    quantity,
    product,
    trigger_price,
    tick_size,
    max_wait_seconds=10,
    poll_interval_seconds=1,
    sleep_fn=None,
    clock_fn=None,
):
    """
    Find a protective order that the broker accepted when the
    place_order response was lost or raised an exception.

    The unique client tag is the primary identity. Order details are
    also checked so an unrelated order cannot be adopted.
    """

    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic

    started = clock_fn()
    attempts = 0
    api_errors = 0
    last_error = None

    expected_tag = str(client_tag)
    expected_symbol = str(symbol).upper()
    expected_exchange = str(exchange).upper()
    expected_side = str(stop_side).upper()
    expected_product = str(product).upper()
    expected_quantity = int(quantity)
    expected_trigger = float(trigger_price)
    trigger_tolerance = max(
        float(tick_size) / 2,
        0.0000001,
    )

    while True:
        attempts += 1

        try:
            broker_orders = kite.orders()
        except Exception as exc:
            broker_orders = []
            api_errors += 1
            last_error = str(exc)

            logger.warning(
                "Protective-stop reconciliation failed "
                "for tag=%s attempt=%s: %s",
                expected_tag,
                attempts,
                exc,
            )

        matches = []

        for order in broker_orders or []:
            try:
                if str(
                    order.get("tag") or ""
                ) != expected_tag:
                    continue

                if str(
                    order.get("tradingsymbol") or ""
                ).upper() != expected_symbol:
                    continue

                if str(
                    order.get("exchange") or ""
                ).upper() != expected_exchange:
                    continue

                if str(
                    order.get("transaction_type") or ""
                ).upper() != expected_side:
                    continue

                order_type = str(
                    order.get("order_type") or ""
                ).upper()

                if order_type not in {"SL-M", "SLM"}:
                    continue

                if str(
                    order.get("product") or ""
                ).upper() != expected_product:
                    continue

                if int(
                    order.get("quantity") or 0
                ) != expected_quantity:
                    continue

                broker_trigger = float(
                    order.get("trigger_price") or 0
                )

                if abs(
                    broker_trigger - expected_trigger
                ) > trigger_tolerance:
                    continue

                if not order.get("order_id"):
                    continue

                matches.append(order)

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                continue

        if len(matches) == 1:
            return {
                "matched": True,
                "order_id": str(
                    matches[0]["order_id"]
                ),
                "order": matches[0],
                "attempts": attempts,
                "api_error_count": api_errors,
                "last_error": last_error,
            }

        if len(matches) > 1:
            raise ProtectiveStopError(
                "Multiple broker orders matched protective "
                f"stop tag {expected_tag}"
            )

        if clock_fn() - started >= max_wait_seconds:
            return {
                "matched": False,
                "order_id": None,
                "order": None,
                "attempts": attempts,
                "api_error_count": api_errors,
                "last_error": last_error,
            }

        sleep_fn(poll_interval_seconds)


def calculate_protective_trigger(
    *,
    confirmed_entry_price: float,
    position_direction: str,
    stop_loss_percent: float,
    tick_size: float,
) -> float:
    entry = Decimal(str(confirmed_entry_price))
    percentage = Decimal(str(stop_loss_percent))
    tick = Decimal(str(tick_size))
    direction = str(position_direction).upper()

    if entry <= 0:
        raise ProtectiveStopError(
            "Confirmed entry price must be positive"
        )

    if percentage <= 0 or percentage >= 100:
        raise ProtectiveStopError(
            "Stop-loss percentage must be between 0 and 100"
        )

    if tick <= 0:
        raise ProtectiveStopError(
            "Tick size must be positive"
        )

    if direction == "BUY":
        raw_trigger = entry * (
            Decimal("1") - percentage / Decimal("100")
        )
    elif direction == "SELL":
        raw_trigger = entry * (
            Decimal("1") + percentage / Decimal("100")
        )
    else:
        raise ProtectiveStopError(
            "Position direction must be BUY or SELL"
        )

    tick_count = (
        raw_trigger / tick
    ).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )

    rounded = tick_count * tick

    if rounded <= 0:
        raise ProtectiveStopError(
            "Calculated trigger price is invalid"
        )

    return float(rounded)


def _parse_latest_record(record, expected_quantity):
    try:
        status = str(
            record.get("status", "")
        ).upper()

        filled = int(
            record.get("filled_quantity") or 0
        )
        pending = int(
            record.get("pending_quantity") or 0
        )
        cancelled = int(
            record.get("cancelled_quantity") or 0
        )

        if min(filled, pending, cancelled) < 0:
            return None

        average_price = record.get("average_price")

        if average_price is not None and filled > 0:
            average_price = float(average_price)
        else:
            average_price = None

        return {
            "status": status,
            "filled_quantity": filled,
            "pending_quantity": pending,
            "cancelled_quantity": cancelled,
            "average_price": average_price,
            "status_message": record.get(
                "status_message"
            ),
            "exchange_order_id": record.get(
                "exchange_order_id"
            ),
            "expected_quantity": expected_quantity,
        }
    except (TypeError, ValueError, AttributeError):
        return None


def _verification_result(
    *,
    order_id,
    expected_quantity,
    parsed,
    state,
    active,
    terminal,
    history_attempts,
    api_error_count,
):
    return ProtectiveStopVerificationResult(
        order_id=str(order_id),
        status=(
            parsed["status"]
            if parsed is not None
            else state
        ),
        state=state,
        requested_quantity=int(expected_quantity),
        filled_quantity=(
            parsed["filled_quantity"]
            if parsed is not None
            else 0
        ),
        pending_quantity=(
            parsed["pending_quantity"]
            if parsed is not None
            else int(expected_quantity)
        ),
        cancelled_quantity=(
            parsed["cancelled_quantity"]
            if parsed is not None
            else 0
        ),
        average_price=(
            parsed["average_price"]
            if parsed is not None
            else None
        ),
        status_message=(
            parsed["status_message"]
            if parsed is not None
            else None
        ),
        exchange_order_id=(
            parsed["exchange_order_id"]
            if parsed is not None
            else None
        ),
        active=active,
        terminal=terminal,
        verified_at=datetime.now(timezone.utc),
        history_attempts=history_attempts,
        api_error_count=api_error_count,
    )


def verify_protective_stop_active(
    kite,
    order_id,
    expected_quantity,
    *,
    max_wait_seconds=15,
    poll_interval_seconds=1,
    sleep_fn=None,
    clock_fn=None,
):
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic

    started = clock_fn()
    attempts = 0
    errors = 0
    latest = None

    while True:
        attempts += 1

        try:
            history = kite.order_history(order_id)
        except Exception as exc:
            errors += 1
            history = None
            logger.warning(
                "Protective-stop order history failed "
                "for %s: %s",
                order_id,
                exc,
            )

        if history:
            parsed = _parse_latest_record(
                history[-1],
                expected_quantity,
            )

            if parsed is not None:
                latest = parsed
            else:
                errors += 1

        if latest is not None:
            status = latest["status"]
            filled = latest["filled_quantity"]

            if status == ACTIVE_STATUS and filled == 0:
                return _verification_result(
                    order_id=order_id,
                    expected_quantity=expected_quantity,
                    parsed=latest,
                    state="ACTIVE",
                    active=True,
                    terminal=False,
                    history_attempts=attempts,
                    api_error_count=errors,
                )

            if status == "COMPLETE":
                state = (
                    "TRIGGERED"
                    if filled >= expected_quantity
                    else "PARTIALLY_TRIGGERED"
                )

                return _verification_result(
                    order_id=order_id,
                    expected_quantity=expected_quantity,
                    parsed=latest,
                    state=state,
                    active=False,
                    terminal=True,
                    history_attempts=attempts,
                    api_error_count=errors,
                )

            if status == "CANCELLED":
                state = (
                    "PARTIALLY_TRIGGERED"
                    if filled > 0
                    else "CANCELLED"
                )

                return _verification_result(
                    order_id=order_id,
                    expected_quantity=expected_quantity,
                    parsed=latest,
                    state=state,
                    active=False,
                    terminal=True,
                    history_attempts=attempts,
                    api_error_count=errors,
                )

            if status == "REJECTED":
                return _verification_result(
                    order_id=order_id,
                    expected_quantity=expected_quantity,
                    parsed=latest,
                    state="REJECTED",
                    active=False,
                    terminal=True,
                    history_attempts=attempts,
                    api_error_count=errors,
                )

        if clock_fn() - started >= max_wait_seconds:
            return _verification_result(
                order_id=order_id,
                expected_quantity=expected_quantity,
                parsed=latest,
                state=(
                    "TIMEOUT"
                    if latest is not None
                    else "UNKNOWN"
                ),
                active=False,
                terminal=False,
                history_attempts=attempts,
                api_error_count=errors,
            )

        sleep_fn(poll_interval_seconds)


def place_protective_stop(
    kite,
    *,
    symbol,
    position_direction,
    quantity,
    exchange,
    confirmed_entry_price,
    stop_loss_percent,
    tick_size,
    cfg,
    entry_operation_id=None,
    store_path=None,
):
    if getattr(cfg, "PAPER_TRADING", True):
        raise ProtectiveStopError(
            "Protective broker stops are live-only"
        )

    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity <= 0
    ):
        raise ProtectiveStopError(
            "Protective-stop quantity must be positive"
        )

    market_protection = getattr(
        cfg,
        "MARKET_PROTECTION",
        None,
    )

    valid_market_protection = (
        market_protection == -1
        or (
            isinstance(market_protection, (int, float))
            and not isinstance(market_protection, bool)
            and 0 < market_protection <= 100
        )
    )

    if not valid_market_protection:
        raise ProtectiveStopError(
            "MARKET_PROTECTION must be -1 or "
            "a percentage greater than 0 and up to 100"
        )

    direction = str(position_direction).upper()

    if direction == "BUY":
        stop_side = "SELL"
        transaction_type = getattr(
            kite,
            "TRANSACTION_TYPE_SELL",
            "SELL",
        )
    elif direction == "SELL":
        stop_side = "BUY"
        transaction_type = getattr(
            kite,
            "TRANSACTION_TYPE_BUY",
            "BUY",
        )
    else:
        raise ProtectiveStopError(
            "Position direction must be BUY or SELL"
        )

    trigger_price = calculate_protective_trigger(
        confirmed_entry_price=confirmed_entry_price,
        position_direction=direction,
        stop_loss_percent=stop_loss_percent,
        tick_size=tick_size,
    )

    client_tag = make_protective_stop_tag()

    operation_id = create_protective_stop_intent(
        symbol=symbol,
        exchange=exchange,
        position_direction=direction,
        stop_side=stop_side,
        requested_quantity=quantity,
        trigger_price=trigger_price,
        client_tag=client_tag,
        tick_size=tick_size,
        entry_operation_id=entry_operation_id,
        path=store_path,
    )

    try:
        order_id = kite.place_order(
            variety=cfg.VARIETY,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=cfg.PRODUCT,
            order_type=getattr(
                kite,
                "ORDER_TYPE_SLM",
                "SL-M",
            ),
            trigger_price=trigger_price,
            validity=getattr(
                kite,
                "VALIDITY_DAY",
                "DAY",
            ),
            market_protection=market_protection,
            tag=client_tag,
        )
    except Exception as exc:
        logger.critical(
            "Protective-stop submission response uncertain for "
            "%s:%s operation=%s tag=%s: %s",
            exchange,
            symbol,
            operation_id,
            client_tag,
            exc,
        )

        reconciliation = (
            reconcile_protective_stop_submission(
                kite,
                client_tag=client_tag,
                symbol=symbol,
                exchange=exchange,
                stop_side=stop_side,
                quantity=quantity,
                product=cfg.PRODUCT,
                trigger_price=trigger_price,
                tick_size=tick_size,
                max_wait_seconds=getattr(
                    cfg,
                    "PROTECTIVE_STOP_RECONCILE_MAX_WAIT_SECONDS",
                    10,
                ),
                poll_interval_seconds=getattr(
                    cfg,
                    "PROTECTIVE_STOP_RECONCILE_POLL_INTERVAL_SECONDS",
                    1,
                ),
            )
        )

        if not reconciliation["matched"]:
            return {
                "success": False,
                "active": False,
                "triggered": False,
                "confirmation_pending": True,
                "status": "SUBMISSION_UNCERTAIN",
                "reason": (
                    "submission exception and no uniquely "
                    "tagged broker order was found: "
                    f"{exc}"
                ),
                "operation_id": operation_id,
                "order_id": None,
                "client_tag": client_tag,
                "trigger_price": trigger_price,
                "requested_quantity": quantity,
                "filled_quantity": 0,
                "average_price": None,
                "reconciliation_attempts": (
                    reconciliation["attempts"]
                ),
                "reconciliation_api_errors": (
                    reconciliation["api_error_count"]
                ),
            }

        order_id = reconciliation["order_id"]

        logger.warning(
            "Recovered protective-stop broker order %s "
            "using unique tag=%s",
            order_id,
            client_tag,
        )

    try:
        attach_protective_stop_order_id(
            operation_id,
            str(order_id),
            path=store_path,
        )
    except Exception as exc:
        logger.critical(
            "Protective-stop persistence uncertain for "
            "%s:%s order=%s operation=%s: %s",
            exchange,
            symbol,
            order_id,
            operation_id,
            exc,
        )

        return {
            "success": False,
            "active": False,
            "triggered": False,
            "confirmation_pending": True,
            "status": "PERSISTENCE_UNCERTAIN",
            "reason": str(exc),
            "operation_id": operation_id,
            "order_id": str(order_id),
            "client_tag": client_tag,
            "trigger_price": trigger_price,
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
        }

    verification = verify_protective_stop_active(
        kite,
        str(order_id),
        quantity,
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

    return {
        "success": (
            verification.active
            or verification.state
            in {"TRIGGERED", "PARTIALLY_TRIGGERED"}
        ),
        "active": verification.active,
        "triggered": verification.state
        in {"TRIGGERED", "PARTIALLY_TRIGGERED"},
        "confirmation_pending": verification.state
        in {"TIMEOUT", "UNKNOWN"},
        "status": verification.status,
        "state": verification.state,
        "reason": verification.status_message,
        "operation_id": operation_id,
        "order_id": str(order_id),
        "client_tag": client_tag,
        "trigger_price": trigger_price,
        "requested_quantity": quantity,
        "filled_quantity": verification.filled_quantity,
        "pending_quantity": verification.pending_quantity,
        "average_price": verification.average_price,
        "verified_at": verification.verified_at.isoformat(),
        "submission_reconciled": (
            "reconciliation" in locals()
            and reconciliation.get("matched", False)
        ),
        "reconciliation_attempts": (
            reconciliation.get("attempts")
            if "reconciliation" in locals()
            else 0
        ),
    }


def recover_protective_stop(
    kite,
    record,
    cfg,
    *,
    store_path=None,
):
    """Resume one durable protective-stop operation without resubmitting.

    An intent without an order ID is reconciled by its unique broker tag.
    An intent with an order ID is verified directly.  Neither path ever
    calls ``place_order``.
    """

    if getattr(cfg, "PAPER_TRADING", True):
        raise ProtectiveStopError(
            "Protective-stop recovery is live-only"
        )

    order_id = record.get("order_id")
    submission_reconciled = False
    reconciliation_attempts = 0

    if order_id is None:
        try:
            reconciliation = reconcile_protective_stop_submission(
                kite,
                client_tag=record["client_tag"],
                symbol=record["symbol"],
                exchange=record["exchange"],
                stop_side=record["stop_side"],
                quantity=int(record["requested_quantity"]),
                product=cfg.PRODUCT,
                trigger_price=float(record["trigger_price"]),
                tick_size=float(record.get("tick_size") or 0.05),
                max_wait_seconds=getattr(
                    cfg,
                    "PROTECTIVE_STOP_RECONCILE_MAX_WAIT_SECONDS",
                    10,
                ),
                poll_interval_seconds=getattr(
                    cfg,
                    "PROTECTIVE_STOP_RECONCILE_POLL_INTERVAL_SECONDS",
                    1,
                ),
            )
        except ProtectiveStopError as exc:
            return {
                "success": False,
                "active": False,
                "triggered": False,
                "confirmation_pending": True,
                "status": "SUBMISSION_AMBIGUOUS",
                "state": "SUBMISSION_AMBIGUOUS",
                "reason": str(exc),
                "operation_id": record["operation_id"],
                "order_id": None,
                "client_tag": record["client_tag"],
                "trigger_price": float(record["trigger_price"]),
                "requested_quantity": int(record["requested_quantity"]),
                "filled_quantity": 0,
                "average_price": None,
                "submission_reconciled": False,
                "reconciliation_attempts": 1,
            }

        reconciliation_attempts = reconciliation["attempts"]

        if not reconciliation["matched"]:
            return {
                "success": False,
                "active": False,
                "triggered": False,
                "confirmation_pending": True,
                "status": "SUBMISSION_UNCERTAIN",
                "state": "SUBMISSION_UNCERTAIN",
                "reason": (
                    "no uniquely tagged broker protective stop was found"
                ),
                "operation_id": record["operation_id"],
                "order_id": None,
                "client_tag": record["client_tag"],
                "trigger_price": float(record["trigger_price"]),
                "requested_quantity": int(record["requested_quantity"]),
                "filled_quantity": 0,
                "average_price": None,
                "submission_reconciled": False,
                "reconciliation_attempts": reconciliation_attempts,
            }

        order_id = reconciliation["order_id"]
        submission_reconciled = True
        attach_protective_stop_order_id(
            record["operation_id"],
            str(order_id),
            path=store_path,
        )

    verification = verify_protective_stop_active(
        kite,
        str(order_id),
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
        record["operation_id"],
        verification,
        path=store_path,
    )

    return {
        "success": (
            verification.active
            or verification.state
            in {"TRIGGERED", "PARTIALLY_TRIGGERED"}
        ),
        "active": verification.active,
        "triggered": verification.state
        in {"TRIGGERED", "PARTIALLY_TRIGGERED"},
        "confirmation_pending": verification.state
        in {"TIMEOUT", "UNKNOWN"},
        "status": verification.status,
        "state": verification.state,
        "reason": verification.status_message,
        "operation_id": record["operation_id"],
        "order_id": str(order_id),
        "client_tag": record["client_tag"],
        "trigger_price": float(record["trigger_price"]),
        "requested_quantity": int(record["requested_quantity"]),
        "filled_quantity": verification.filled_quantity,
        "pending_quantity": verification.pending_quantity,
        "average_price": verification.average_price,
        "verified_at": verification.verified_at.isoformat(),
        "submission_reconciled": submission_reconciled,
        "reconciliation_attempts": reconciliation_attempts,
    }
