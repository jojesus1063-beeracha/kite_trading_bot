"""
Durable, atomic persistence for unresolved F&O order intents and
submitted broker order IDs -- satisfies spec #27 (crash recovery) and
spec #28 (duplicate-order protection: a timeout is never treated as
proof an order failed; a retry is never blind).

This is a deliberate, path-scoped duplicate of the equity bot's
pending_order_store.py, not an import from it -- see architecture
review Section B ("must remain separate", point 2): if this file
accidentally reused the equity bot's STORE_PATH/lock file, duplicate-
order protection would silently break across BOTH bots. The
path-distinctness is verified by a test (tests/test_order_store.py).

Concurrency: real POSIX file lock (fcntl.flock) on a companion .lock
file guards the read-modify-write sequence in every mutating function,
same discipline as the equity bot's module.
"""
import json
import os
import uuid
import fcntl
import logging
import contextlib
from datetime import datetime

logger = logging.getLogger("fno.order_store")

STORE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_pending_orders.json")

VALID_ACTIONS = {"ENTRY", "EXIT", "FORCE_EXIT"}
VALID_SIDES = {"BUY", "SELL"}
EXIT_FAMILY = {"EXIT", "FORCE_EXIT"}


class OrderStoreError(Exception):
    """Raised for corrupt store data or invalid record operations --
    never silently swallowed or auto-repaired."""


class DuplicateOperationError(OrderStoreError):
    pass


class DuplicateBrokerOrderIdError(OrderStoreError):
    pass


class UnresolvedOrderExistsError(OrderStoreError):
    pass


class InvalidOrderRecordError(OrderStoreError):
    pass


@contextlib.contextmanager
def _file_lock(path):
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


def _validate_average_price(average_price, filled_quantity):
    if average_price is not None and filled_quantity == 0:
        raise InvalidOrderRecordError("average_price present with filled_quantity == 0 (never fabricated)")


def load_pending_orders(path=None):
    p = path if path is not None else STORE_PATH
    if not os.path.exists(p):
        return {"schema_version": 1, "snapshot_id": str(uuid.uuid4()),
                "generated_at": datetime.now().isoformat(), "orders": []}
    try:
        with open(p) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise OrderStoreError(f"fno order store at {p} is corrupt JSON: {e}") from e
    if not isinstance(data, dict) or "orders" not in data:
        raise OrderStoreError(f"fno order store at {p} has an unexpected structure (missing 'orders')")
    return data


def save_pending_orders(data, path=None):
    from fno_bot.json_safe import json_safe
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
    data = _data if _data is not None else load_pending_orders(path)
    relevant = {"ENTRY"} if action == "ENTRY" else EXIT_FAMILY
    for order in data["orders"]:
        if (order["symbol"] == symbol and order["exchange"] == exchange
                and not order["resolved"] and order["action"] in relevant):
            return True
    return False


def create_order_intent(symbol, exchange, action, side, requested_quantity, path=None):
    _validate_intent_inputs(symbol, exchange, action, side, requested_quantity)
    p = path if path is not None else STORE_PATH
    with _file_lock(p):
        data = load_pending_orders(p)
        if has_unresolved_order(symbol, exchange, action, _data=data):
            raise UnresolvedOrderExistsError(
                f"An unresolved order already blocks a new {action} for {exchange}:{symbol}")
        operation_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        record = {
            "operation_id": operation_id, "order_id": None, "symbol": symbol, "exchange": exchange,
            "action": action, "side": side, "requested_quantity": requested_quantity,
            "filled_quantity": 0, "pending_quantity": requested_quantity, "cancelled_quantity": 0,
            "average_price": None, "last_known_status": "INTENT_CREATED", "terminal": False,
            "resolved": False, "status_message": None, "exchange_order_id": None,
            "submitted_at": None, "created_at": now, "last_checked_at": None, "updated_at": now,
            "verification_attempts": 0, "api_error_count": 0,
        }
        data["orders"].append(record)
        save_pending_orders(data, p)
        return operation_id


def attach_broker_order_id(operation_id, order_id, path=None):
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


def list_unresolved_orders(path=None):
    data = load_pending_orders(path)
    return [o for o in data["orders"] if not o["resolved"]]
