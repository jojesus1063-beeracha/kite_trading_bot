"""Durable storage for live broker-side protective stop orders."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORE_PATH = Path(__file__).resolve().with_name(
    "protective_stops.json"
)
SCHEMA_VERSION = 1


class ProtectiveStopStoreError(Exception):
    pass


class UnresolvedProtectiveStopExistsError(
    ProtectiveStopStoreError
):
    pass


class InvalidProtectiveStopRecordError(
    ProtectiveStopStoreError
):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(path=None) -> Path:
    return Path(path) if path is not None else STORE_PATH


def _fresh_store() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "stops": [],
    }


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")

    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_protective_stops(path=None) -> dict[str, Any]:
    store_path = _path(path)

    if not store_path.exists():
        return _fresh_store()

    try:
        data = json.loads(
            store_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ProtectiveStopStoreError(
            f"Cannot read protective-stop store: {exc}"
        ) from exc

    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProtectiveStopStoreError(
            "Unsupported protective-stop store schema"
        )

    if not isinstance(data.get("stops"), list):
        raise ProtectiveStopStoreError(
            "Protective-stop store has no valid stops list"
        )

    return data


def save_protective_stops(
    data: dict[str, Any],
    path=None,
) -> None:
    store_path = _path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)

    data["schema_version"] = SCHEMA_VERSION
    data["generated_at"] = _now()

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{store_path.name}.",
        suffix=".tmp",
        dir=store_path.parent,
        text=True,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                data,
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, store_path)

        directory_fd = os.open(
            store_path.parent,
            os.O_RDONLY,
        )

        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)


def _find(
    data: dict[str, Any],
    operation_id: str,
) -> dict[str, Any] | None:
    for record in data["stops"]:
        if record["operation_id"] == operation_id:
            return record

    return None


def list_unresolved_protective_stops(
    path=None,
) -> list[dict[str, Any]]:
    return [
        record
        for record in load_protective_stops(path)["stops"]
        if not record["resolved"]
    ]


def has_unresolved_protective_stop(
    symbol: str,
    exchange: str,
    path=None,
    *,
    data=None,
) -> bool:
    snapshot = (
        data
        if data is not None
        else load_protective_stops(path)
    )

    return any(
        record["symbol"] == symbol
        and record["exchange"] == exchange
        and not record["resolved"]
        for record in snapshot["stops"]
    )


def create_protective_stop_intent(
    *,
    symbol: str,
    exchange: str,
    position_direction: str,
    stop_side: str,
    requested_quantity: int,
    trigger_price: float,
    client_tag: str,
    path=None,
) -> str:
    direction = str(position_direction).upper()
    side = str(stop_side).upper()

    if not symbol or not exchange:
        raise InvalidProtectiveStopRecordError(
            "symbol and exchange are required"
        )

    if direction not in {"BUY", "SELL"}:
        raise InvalidProtectiveStopRecordError(
            "position direction must be BUY or SELL"
        )

    if side not in {"BUY", "SELL"}:
        raise InvalidProtectiveStopRecordError(
            "stop side must be BUY or SELL"
        )

    if side == direction:
        raise InvalidProtectiveStopRecordError(
            "protective-stop side must oppose position direction"
        )

    if (
        not isinstance(requested_quantity, int)
        or isinstance(requested_quantity, bool)
        or requested_quantity <= 0
    ):
        raise InvalidProtectiveStopRecordError(
            "requested quantity must be a positive integer"
        )

    if float(trigger_price) <= 0:
        raise InvalidProtectiveStopRecordError(
            "trigger price must be positive"
        )

    if (
        not isinstance(client_tag, str)
        or not client_tag
        or not client_tag.isalnum()
        or len(client_tag) > 20
    ):
        raise InvalidProtectiveStopRecordError(
            "client tag must be alphanumeric "
            "and no longer than 20 characters"
        )

    store_path = _path(path)

    with _file_lock(store_path):
        data = load_protective_stops(store_path)

        if has_unresolved_protective_stop(
            symbol,
            exchange,
            data=data,
        ):
            raise UnresolvedProtectiveStopExistsError(
                f"Unresolved protective stop already exists "
                f"for {exchange}:{symbol}"
            )

        operation_id = str(uuid.uuid4())
        now = _now()

        data["stops"].append({
            "operation_id": operation_id,
            "order_id": None,
            "client_tag": client_tag,
            "symbol": symbol,
            "exchange": exchange,
            "position_direction": direction,
            "stop_side": side,
            "requested_quantity": requested_quantity,
            "trigger_price": float(trigger_price),
            "filled_quantity": 0,
            "pending_quantity": requested_quantity,
            "cancelled_quantity": 0,
            "average_price": None,
            "last_known_status": "INTENT_CREATED",
            "active": False,
            "terminal": False,
            "resolved": False,
            "status_message": None,
            "exchange_order_id": None,
            "submitted_at": None,
            "created_at": now,
            "last_checked_at": None,
            "updated_at": now,
            "verification_attempts": 0,
            "api_error_count": 0,
        })

        save_protective_stops(data, store_path)

    return operation_id


def attach_protective_stop_order_id(
    operation_id: str,
    order_id: str,
    path=None,
) -> None:
    if not order_id:
        raise InvalidProtectiveStopRecordError(
            "broker order ID is required"
        )

    store_path = _path(path)

    with _file_lock(store_path):
        data = load_protective_stops(store_path)
        record = _find(data, operation_id)

        if record is None:
            raise InvalidProtectiveStopRecordError(
                f"Unknown operation ID: {operation_id}"
            )

        existing = record.get("order_id")

        if existing not in (None, order_id):
            raise InvalidProtectiveStopRecordError(
                "Conflicting broker order ID"
            )

        for other in data["stops"]:
            if (
                other["operation_id"] != operation_id
                and other.get("order_id") == order_id
            ):
                raise InvalidProtectiveStopRecordError(
                    "Broker order ID is already attached "
                    "to another protective stop"
                )

        record["order_id"] = order_id
        record["submitted_at"] = _now()
        record["updated_at"] = _now()

        save_protective_stops(data, store_path)


def update_protective_stop_verification(
    operation_id: str,
    result,
    path=None,
) -> None:
    store_path = _path(path)

    with _file_lock(store_path):
        data = load_protective_stops(store_path)
        record = _find(data, operation_id)

        if record is None:
            raise InvalidProtectiveStopRecordError(
                f"Unknown operation ID: {operation_id}"
            )

        filled = int(result.filled_quantity)
        pending = int(result.pending_quantity)
        cancelled = int(result.cancelled_quantity)

        if min(filled, pending, cancelled) < 0:
            raise InvalidProtectiveStopRecordError(
                "Broker quantities cannot be negative"
            )

        if filled > int(record["requested_quantity"]):
            raise InvalidProtectiveStopRecordError(
                "Filled quantity exceeds requested quantity"
            )

        record["filled_quantity"] = filled
        record["pending_quantity"] = pending
        record["cancelled_quantity"] = cancelled
        record["average_price"] = result.average_price
        record["last_known_status"] = result.status
        record["active"] = bool(result.active)
        record["terminal"] = bool(result.terminal)
        record["status_message"] = result.status_message
        record["exchange_order_id"] = (
            result.exchange_order_id
        )
        record["last_checked_at"] = (
            result.verified_at.isoformat()
        )
        record["updated_at"] = _now()
        record["verification_attempts"] += int(
            result.history_attempts
        )
        record["api_error_count"] += int(
            result.api_error_count
        )

        save_protective_stops(data, store_path)


def mark_protective_stop_resolved(
    operation_id: str,
    *,
    resolution_reason: str | None = None,
    path=None,
) -> None:
    store_path = _path(path)

    with _file_lock(store_path):
        data = load_protective_stops(store_path)
        record = _find(data, operation_id)

        if record is None:
            raise InvalidProtectiveStopRecordError(
                f"Unknown operation ID: {operation_id}"
            )

        record["resolved"] = True
        record["active"] = False
        record["updated_at"] = _now()

        if resolution_reason:
            record["status_message"] = resolution_reason

        save_protective_stops(data, store_path)


def get_protective_stop(
    operation_id: str,
    path=None,
) -> dict[str, Any] | None:
    return _find(
        load_protective_stops(path),
        operation_id,
    )
