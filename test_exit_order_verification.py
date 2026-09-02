import os
import tempfile
from types import SimpleNamespace

import pending_order_store
from executor import place_exit_order as _place_exit_order


def place_exit_order(
    kite,
    symbol,
    direction,
    quantity,
    exchange,
    cfg,
):
    """Exercise executor verification after stop clearance."""

    clearance = {
        "safe_to_submit_exit": True,
        "paper": False,
        "symbol": symbol,
        "exchange": exchange,
        "quantity": quantity,
        "exit_action": "EXIT",
        "protective_stop_state": "CANCELLED",
    }
    return _place_exit_order(
        kite,
        symbol,
        direction,
        quantity,
        exchange,
        cfg,
        protection_clearance=clearance,
    )


passed = 0
failed = 0


def check(name, condition):
    global passed, failed

    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(
        self,
        history_record=None,
        place_exception=None,
        order_id="EXIT-ORDER-1",
    ):
        self.history_record = history_record
        self.place_exception = place_exception
        self.order_id = order_id
        self.place_calls = 0
        self.history_calls = 0

    def place_order(self, **kwargs):
        self.place_calls += 1

        if self.place_exception is not None:
            raise self.place_exception

        return self.order_id

    def order_history(self, order_id):
        self.history_calls += 1

        if self.history_record is None:
            return []

        return [dict(self.history_record)]


def live_cfg():
    return SimpleNamespace(
        PAPER_TRADING=False,
        VARIETY="regular",
        PRODUCT="MIS",
        MARKET_PROTECTION=0,
        ORDER_VERIFY_MAX_WAIT_SECONDS=0,
        ORDER_VERIFY_POLL_INTERVAL_SECONDS=0,
    )


def paper_cfg():
    return SimpleNamespace(
        PAPER_TRADING=True,
        VARIETY="regular",
        PRODUCT="MIS",
        MARKET_PROTECTION=0,
        ORDER_VERIFY_MAX_WAIT_SECONDS=0,
        ORDER_VERIFY_POLL_INTERVAL_SECONDS=0,
    )


def run_isolated(test_fn):
    original_store = pending_order_store.STORE_PATH

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )

        try:
            test_fn()
        finally:
            pending_order_store.STORE_PATH = original_store


def test_complete_fill():
    kite = FakeKite(
        {
            "status": "COMPLETE",
            "filled_quantity": 10,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 105.25,
            "status_message": None,
            "exchange_order_id": "EX-1",
        }
    )

    result = place_exit_order(
        kite,
        "TEST",
        "BUY",
        10,
        "NSE",
        live_cfg(),
    )

    check("1. Complete exit returns success", result["success"])
    check(
        "1b. Complete exit returns confirmed quantity",
        result["filled_quantity"] == 10,
    )
    check(
        "1c. Complete exit uses broker average price",
        result["average_price"] == 105.25,
    )
    check(
        "1d. Complete exit is resolved",
        result["resolved"] is True,
    )

    stored = pending_order_store.get_order(
        result["operation_id"]
    )

    check(
        "1e. Complete broker fill remains unresolved in the store "
        "until main.py applies the local position change",
        stored["resolved"] is False,
    )


def test_terminal_partial():
    kite = FakeKite(
        {
            "status": "CANCELLED",
            "filled_quantity": 4,
            "pending_quantity": 0,
            "cancelled_quantity": 6,
            "average_price": 104.75,
            "status_message": "remaining quantity cancelled",
            "exchange_order_id": "EX-2",
        },
        order_id="EXIT-ORDER-2",
    )

    result = place_exit_order(
        kite,
        "TESTPART",
        "BUY",
        10,
        "NSE",
        live_cfg(),
    )

    check(
        "2. Terminal partial exit returns success",
        result["success"] is True,
    )
    check(
        "2b. Only 4 confirmed shares are returned",
        result["filled_quantity"] == 4,
    )
    check(
        "2c. Terminal partial status is correct",
        result["status"] == "PARTIALLY_FILLED",
    )
    check(
        "2d. Terminal partial is resolved",
        result["resolved"] is True,
    )


def test_rejected():
    kite = FakeKite(
        {
            "status": "REJECTED",
            "filled_quantity": 0,
            "pending_quantity": 0,
            "cancelled_quantity": 10,
            "average_price": 0,
            "status_message": "broker rejected",
            "exchange_order_id": None,
        },
        order_id="EXIT-ORDER-3",
    )

    result = place_exit_order(
        kite,
        "TESTREJ",
        "SELL",
        10,
        "NSE",
        live_cfg(),
    )

    check(
        "3. Rejected exit does not report success",
        result["success"] is False,
    )
    check(
        "3b. Rejected exit reports zero fill",
        result["filled_quantity"] == 0,
    )
    check(
        "3c. Rejected exit is resolved",
        result["resolved"] is True,
    )


def test_timeout_partial():
    kite = FakeKite(
        {
            "status": "OPEN",
            "filled_quantity": 3,
            "pending_quantity": 7,
            "cancelled_quantity": 0,
            "average_price": 103.50,
            "status_message": None,
            "exchange_order_id": "EX-4",
        },
        order_id="EXIT-ORDER-4",
    )

    result = place_exit_order(
        kite,
        "TESTTIMEPART",
        "BUY",
        10,
        "NSE",
        live_cfg(),
    )

    check(
        "4. Timeout partial reports confirmed fill",
        result["filled_quantity"] == 3,
    )
    check(
        "4b. Timeout partial remains unresolved",
        result["resolved"] is False,
    )
    check(
        "4c. Timeout partial blocks as confirmation pending",
        result["exit_confirmation_pending"] is True,
    )


def test_timeout_zero_and_duplicate_block():
    kite = FakeKite(
        {
            "status": "OPEN",
            "filled_quantity": 0,
            "pending_quantity": 10,
            "cancelled_quantity": 0,
            "average_price": 0,
            "status_message": None,
            "exchange_order_id": "EX-5",
        },
        order_id="EXIT-ORDER-5",
    )

    first = place_exit_order(
        kite,
        "TESTDUP",
        "BUY",
        10,
        "NSE",
        live_cfg(),
    )

    second = place_exit_order(
        kite,
        "TESTDUP",
        "BUY",
        10,
        "NSE",
        live_cfg(),
    )

    check(
        "5. Zero-fill timeout remains unresolved",
        first["resolved"] is False,
    )
    check(
        "5b. Duplicate unresolved exit is blocked",
        second["status"] == "EXIT_BLOCKED_PENDING_ORDER",
    )
    check(
        "5c. Broker received only one exit submission",
        kite.place_calls == 1,
    )


def test_submission_uncertain():
    kite = FakeKite(
        place_exception=TimeoutError(
            "network timeout during submission"
        )
    )

    first = place_exit_order(
        kite,
        "TESTUNCERTAIN",
        "SELL",
        5,
        "NSE",
        live_cfg(),
    )

    second = place_exit_order(
        kite,
        "TESTUNCERTAIN",
        "SELL",
        5,
        "NSE",
        live_cfg(),
    )

    check(
        "6. Submission exception becomes SUBMISSION_UNCERTAIN",
        first["status"] == "SUBMISSION_UNCERTAIN",
    )
    check(
        "6b. Submission-uncertain intent remains unresolved",
        first["resolved"] is False,
    )
    check(
        "6c. Submission-uncertain operation has no order ID",
        first["order_id"] is None,
    )
    check(
        "6d. Blind retry is blocked",
        second["status"] == "EXIT_BLOCKED_PENDING_ORDER",
    )
    check(
        "6e. Broker submission was attempted only once",
        kite.place_calls == 1,
    )


def test_paper_isolation():
    kite = FakeKite()

    result = place_exit_order(
        kite,
        "TESTPAPER",
        "BUY",
        7,
        "NSE",
        paper_cfg(),
    )

    check(
        "7. Paper exit returns synthetic full fill",
        result["filled_quantity"] == 7,
    )
    check(
        "7b. Paper exit does not call place_order",
        kite.place_calls == 0,
    )
    check(
        "7c. Paper exit does not call order_history",
        kite.history_calls == 0,
    )
    check(
        "7d. Paper exit does not create pending_orders.json",
        not os.path.exists(pending_order_store.STORE_PATH),
    )


print("--- Stage 4 Executor Exit Verification ---")

run_isolated(test_complete_fill)
run_isolated(test_terminal_partial)
run_isolated(test_rejected)
run_isolated(test_timeout_partial)
run_isolated(test_timeout_zero_and_duplicate_block)
run_isolated(test_submission_uncertain)
run_isolated(test_paper_isolation)

print()
print(
    "Results: "
    + str(passed)
    + " passed, "
    + str(failed)
    + " failed"
)

if failed:
    raise SystemExit(1)
