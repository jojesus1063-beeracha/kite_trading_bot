"""Live-only Zerodha broker-side protective SL-M engine."""

from __future__ import annotations

import logging
import time
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

    operation_id = create_protective_stop_intent(
        symbol=symbol,
        exchange=exchange,
        position_direction=direction,
        stop_side=stop_side,
        requested_quantity=quantity,
        trigger_price=trigger_price,
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
            tag="kitebot-stop",
        )
    except Exception as exc:
        logger.critical(
            "Protective-stop submission uncertain for "
            "%s:%s operation=%s: %s",
            exchange,
            symbol,
            operation_id,
            exc,
        )

        return {
            "success": False,
            "active": False,
            "triggered": False,
            "confirmation_pending": True,
            "status": "SUBMISSION_UNCERTAIN",
            "reason": str(exc),
            "operation_id": operation_id,
            "order_id": None,
            "trigger_price": trigger_price,
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
        }

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
        "trigger_price": trigger_price,
        "requested_quantity": quantity,
        "filled_quantity": verification.filled_quantity,
        "pending_quantity": verification.pending_quantity,
        "average_price": verification.average_price,
        "verified_at": verification.verified_at.isoformat(),
    }
