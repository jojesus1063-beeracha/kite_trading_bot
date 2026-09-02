"""
Durable, atomic persistence for order intents, broker order IDs and
locally applied entry-fill quantities.  The executor and restart path
use this journal to prevent duplicate broker submissions and to recover
confirmed fills across process failure.

Concurrency: file-level atomic writes (temp file + fsync + os.replace)
protect against corruption from a single process crashing mid-write.
They do NOT by themselves prevent a race between two SIMULTANEOUS
processes both reading, modifying, and writing the store at once --
that requires holding a lock across the full read-modify-write
sequence, which every mutating function here does via a real,
dependency-free POSIX file lock (fcntl.flock) on a companion .lock
file. This assumes a single bot instance per machine (already true in
production: kitebot.service's Type=simple ensures systemd never runs
two copies concurrently) -- the lock protects against secondary
sources of concurrent access (e.g. a manual script run while the bot
is live), not a multi-machine deployment, which is out of scope.
"""
import json
import os
import time
import uuid
import fcntl
import logging
import contextlib
from datetime import datetime

logger = logging.getLogger("pending_order_store")

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_orders.json")

VALID_ACTIONS = {"ENTRY", "EXIT", "FORCE_EXIT"}
VALID_SIDES = {"BUY", "SELL"}
EXIT_FAMILY = {"EXIT", "FORCE_EXIT"}  # either blocks the other, per the explicit spec rule


class PendingOrderStoreError(Exception):
    """Raised for corrupt store data or invalid record operations --
    never silently swallowed or auto-repaired."""


class DuplicateOperationError(PendingOrderStoreError):
    pass


class DuplicateBrokerOrderIdError(PendingOrderStoreError):
    pass


class UnresolvedOrderExistsError(PendingOrderStoreError):
    pass


class InvalidOrderRecordError(PendingOrderStoreError):
    pass


@contextlib.contextmanager
def _file_lock(path):
    """Real inter-process lock via a companion .lock file (fcntl.flock,
    POSIX, no new dependency). Blocks until acquired -- callers doing
    a read-modify-write MUST wrap the whole sequence in this, not just
    the write, or the lock provides no real protection."""
    lock_path = path + ".lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _validate_intent_inputs(symbol, exchange, action, side, requested_quantity):
    if not symbol or not isinstance(symbol, str):
        raise InvalidOrderRecordError("symbol must be a non-empty string")
    if not exchange or not isinstance(exchange, str):
        raise InvalidOrderRecordError("exchange must be a non-empty string")
    if action not in VALID_ACTIONS:
        raise InvalidOrderRecordError(f"action must be one of {VALID_ACTIONS}, got {action!r}")
    if side not in VALID_SIDES:
        raise InvalidOrderRecordError(f"side must be one of {VALID_SIDES}, got {side!r}")
    if not isinstance(requested_quantity, int) or isinstance(requested_quantity, bool) or requested_quantity <= 0:
        raise InvalidOrderRecordError(f"requested_quantity must be a positive int, got {requested_quantity!r}")


def _validate_quantities(requested, filled, pending, cancelled):
    for name, val in [("filled_quantity", filled), ("pending_quantity", pending), ("cancelled_quantity", cancelled)]:
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise InvalidOrderRecordError(f"{name} must be a non-negative int, got {val!r}")
    if filled > requested:
        raise InvalidOrderRecordError(f"filled_quantity ({filled}) cannot exceed requested_quantity ({requested})")


def _validate_average_price(average_price, filled_quantity, justified=False):
    if average_price is not None and filled_quantity == 0 and not justified:
        raise InvalidOrderRecordError("average_price present with filled_quantity == 0 (never fabricated, unless explicitly justified)")


def load_pending_orders(path=None):
    """
    Returns the full store dict {schema_version, snapshot_id,
    generated_at, orders: [...]}. Missing file -> a fresh empty store
    (not an error). Corrupt JSON -> raises PendingOrderStoreError
    explicitly, NEVER silently treated as empty -- a corrupt store is
    a real data-integrity problem that must surface loudly, not be
    swallowed.
    """
    p = path if path is not None else STORE_PATH
    if not os.path.exists(p):
        return {"schema_version": 1, "snapshot_id": str(uuid.uuid4()),
                "generated_at": datetime.now().isoformat(), "orders": []}
    try:
        with open(p) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise PendingOrderStoreError(f"pending_orders store at {p} is corrupt JSON: {e}") from e
    if not isinstance(data, dict) or "orders" not in data:
        raise PendingOrderStoreError(f"pending_orders store at {p} has an unexpected structure (missing 'orders')")
    return data


def save_pending_orders(data, path=None):
    """
    Atomic write: same-directory temp file, flush, fsync, os.replace.
    Exception-safe temp-file cleanup. Refreshes snapshot_id/
    generated_at on every write.
    """
    from json_safe import json_safe
    p = path if path is not None else STORE_PATH
    data = dict(data)
    data["snapshot_id"] = str(uuid.uuid4())
    data["generated_at"] = datetime.now().isoformat()

    tmp_path = p + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(json_safe(data), f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _find_by_operation_id(data, operation_id):
    for order in data["orders"]:
        if order["operation_id"] == operation_id:
            return order
    return None


def has_unresolved_order(symbol, exchange, action, path=None, _data=None):
    """
    ENTRY only checks against other unresolved ENTRY records for the
    same symbol+exchange. EXIT and FORCE_EXIT are one family -- an
    unresolved order of EITHER kind blocks a new attempt of EITHER
    kind, per the explicit rule that an unresolved exit must block
    both further EXIT and FORCE_EXIT duplicates.
    """
    data = _data if _data is not None else load_pending_orders(path)
    relevant = {"ENTRY"} if action == "ENTRY" else EXIT_FAMILY
    for order in data["orders"]:
        if (order["symbol"] == symbol and order["exchange"] == exchange
                and not order["resolved"] and order["action"] in relevant):
            return True
    return False


def create_order_intent(
    symbol,
    exchange,
    action,
    side,
    requested_quantity,
    path=None,
    *,
    client_tag=None,
    metadata=None,
):
    """
    Validates inputs, checks idempotency (raises UnresolvedOrderExistsError
    if a blocking unresolved order already exists for this symbol+
    exchange+action-family), persists the intent atomically under the
    store's lock, and returns the new operation_id. order_id starts
    as None -- the intent exists durably BEFORE any broker submission.
    """
    _validate_intent_inputs(symbol, exchange, action, side, requested_quantity)

    if client_tag is not None:
        if (
            not isinstance(client_tag, str)
            or not client_tag
            or not client_tag.isalnum()
            or len(client_tag) > 20
        ):
            raise InvalidOrderRecordError(
                "client_tag must be an alphanumeric string no longer than 20 characters"
            )

    if metadata is None:
        metadata = {}
    elif not isinstance(metadata, dict):
        raise InvalidOrderRecordError("metadata must be a dict")
    p = path if path is not None else STORE_PATH
    with _file_lock(p):
        data = load_pending_orders(p)
        if has_unresolved_order(symbol, exchange, action, _data=data):
            raise UnresolvedOrderExistsError(
                f"An unresolved order already blocks a new {action} for {exchange}:{symbol}")
        operation_id = str(uuid.uuid4())
        if _find_by_operation_id(data, operation_id) is not None:
            raise DuplicateOperationError(f"operation_id collision: {operation_id}")
        now = datetime.now().isoformat()
        record = {
            "operation_id": operation_id, "order_id": None, "client_tag": client_tag,
            "symbol": symbol, "exchange": exchange,
            "action": action, "side": side, "requested_quantity": requested_quantity,
            "filled_quantity": 0, "pending_quantity": requested_quantity, "cancelled_quantity": 0,
            "applied_filled_quantity": 0, "metadata": dict(metadata),
            "average_price": None, "last_known_status": "INTENT_CREATED", "terminal": False,
            "resolved": False, "status_message": None, "exchange_order_id": None,
            "submitted_at": None, "created_at": now, "last_checked_at": None, "updated_at": now,
            "verification_attempts": 0, "api_error_count": 0,
        }
        data["orders"].append(record)
        save_pending_orders(data, p)
        return operation_id


def attach_broker_order_id(operation_id, order_id, path=None):
    """
    Idempotent when called again with the SAME order_id. Raises
    DuplicateBrokerOrderIdError if a DIFFERENT order_id is already
    attached, or if the given order_id is already claimed by a
    different operation_id -- never silently overwritten.
    """
    p = path if path is not None else STORE_PATH
    with _file_lock(p):
        data = load_pending_orders(p)
        record = _find_by_operation_id(data, operation_id)
        if record is None:
            raise InvalidOrderRecordError(f"No pending order with operation_id={operation_id}")
        if record["order_id"] is not None and record["order_id"] != order_id:
            raise DuplicateBrokerOrderIdError(
                f"operation_id={operation_id} already has order_id={record['order_id']}, "
                f"cannot attach conflicting order_id={order_id}")
        for other in data["orders"]:
            if other["operation_id"] != operation_id and other["order_id"] == order_id:
                raise DuplicateBrokerOrderIdError(
                    f"order_id={order_id} is already attached to a different operation_id={other['operation_id']}")
        record["order_id"] = order_id
        record["submitted_at"] = datetime.now().isoformat()
        record["updated_at"] = datetime.now().isoformat()
        save_pending_orders(data, p)


def update_order_verification(operation_id, exec_result, path=None):
    """
    Updates a pending order record from an OrderExecutionResult
    (order_verification.py). Does NOT set resolved -- resolution is a
    separate, deliberate step via mark_order_resolved(), so a caller
    always has a chance to inspect the terminal verification result
    before committing to closing/updating local position state.
    """
    p = path if path is not None else STORE_PATH
    with _file_lock(p):
        data = load_pending_orders(p)
        record = _find_by_operation_id(data, operation_id)
        if record is None:
            raise InvalidOrderRecordError(f"No pending order with operation_id={operation_id}")
        _validate_quantities(record["requested_quantity"], exec_result.filled_quantity,
                             exec_result.pending_quantity, exec_result.cancelled_quantity)
        _validate_average_price(exec_result.average_price, exec_result.filled_quantity)
        record["filled_quantity"] = exec_result.filled_quantity
        record["pending_quantity"] = exec_result.pending_quantity
        record["cancelled_quantity"] = exec_result.cancelled_quantity
        record["average_price"] = exec_result.average_price
        record["last_known_status"] = exec_result.status
        record["terminal"] = exec_result.terminal
        record["status_message"] = exec_result.status_message
        record["exchange_order_id"] = exec_result.exchange_order_id
        record["last_checked_at"] = datetime.now().isoformat()
        record["updated_at"] = datetime.now().isoformat()
        record["verification_attempts"] += exec_result.history_attempts
        record["api_error_count"] += exec_result.api_error_count
        save_pending_orders(data, p)


def mark_order_resolved(operation_id, resolution_reason=None, path=None):
    p = path if path is not None else STORE_PATH
    with _file_lock(p):
        data = load_pending_orders(p)
        record = _find_by_operation_id(data, operation_id)
        if record is None:
            raise InvalidOrderRecordError(f"No pending order with operation_id={operation_id}")
        record["resolved"] = True
        if resolution_reason:
            record["status_message"] = resolution_reason
        record["updated_at"] = datetime.now().isoformat()
        save_pending_orders(data, p)


def get_order(operation_id, path=None):
    return _find_by_operation_id(load_pending_orders(path), operation_id)


def get_order_by_broker_id(order_id, path=None):
    data = load_pending_orders(path)
    for order in data["orders"]:
        if order["order_id"] == order_id:
            return order
    return None


def list_unresolved_orders(path=None):
    data = load_pending_orders(path)
    return [o for o in data["orders"] if not o["resolved"]]


def list_entry_orders_requiring_local_application(path=None):
    """Return ENTRY records whose confirmed fills are not fully local.

    This deliberately includes terminal/resolved broker orders.  A process
    can crash after broker verification but before ``open_positions.json``
    is persisted; the durable order record must still make that fill visible
    on restart.
    """

    data = load_pending_orders(path)
    return [
        order
        for order in data["orders"]
        if order.get("action") == "ENTRY"
        and int(order.get("filled_quantity") or 0)
        > int(order.get("applied_filled_quantity") or 0)
    ]


def mark_entry_fill_applied(operation_id, applied_quantity, path=None):
    """Persist how much of a confirmed ENTRY fill exists in local state."""

    if (
        not isinstance(applied_quantity, int)
        or isinstance(applied_quantity, bool)
        or applied_quantity < 0
    ):
        raise InvalidOrderRecordError(
            "applied_quantity must be a non-negative int"
        )

    p = path if path is not None else STORE_PATH

    with _file_lock(p):
        data = load_pending_orders(p)
        record = _find_by_operation_id(data, operation_id)

        if record is None:
            raise InvalidOrderRecordError(
                f"No pending order with operation_id={operation_id}"
            )

        if record.get("action") != "ENTRY":
            raise InvalidOrderRecordError(
                "Only ENTRY operations can record applied fills"
            )

        confirmed = int(record.get("filled_quantity") or 0)
        previous = int(record.get("applied_filled_quantity") or 0)

        if applied_quantity < previous:
            raise InvalidOrderRecordError(
                "applied entry quantity cannot move backwards"
            )

        if applied_quantity > confirmed:
            raise InvalidOrderRecordError(
                "applied entry quantity exceeds broker-confirmed quantity"
            )

        record["applied_filled_quantity"] = applied_quantity
        record["updated_at"] = datetime.now().isoformat()
        save_pending_orders(data, p)
