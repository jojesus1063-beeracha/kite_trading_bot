import os
import tempfile
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import main as main_module
import pending_order_store

from order_verification import OrderExecutionResult


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


class FakeRisk:
    def __init__(self):
        self.results = []

    def record_trade_result(self, pnl):
        self.results.append(pnl)


class FakeKite:
    def __init__(self, record):
        self.record = record
        self.history_calls = 0

    def order_history(self, order_id):
        self.history_calls += 1
        return [deepcopy(self.record)]


def cfg():
    return SimpleNamespace(
        PAPER_TRADING=False,
        ORDER_VERIFY_MAX_WAIT_SECONDS=0,
        ORDER_VERIFY_POLL_INTERVAL_SECONDS=0,
    )


def execution_result(
    order_id,
    status,
    requested,
    filled,
    pending,
    cancelled,
    average_price,
    terminal,
):
    return OrderExecutionResult(
        order_id=order_id,
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        pending_quantity=pending,
        cancelled_quantity=cancelled,
        average_price=average_price,
        status_message=None,
        exchange_order_id="EXCHANGE-ORDER",
        terminal=terminal,
        verified_at=datetime.now(),
        history_attempts=1,
        api_error_count=0,
    )


def create_submitted_exit(
    symbol,
    quantity,
    order_id,
):
    operation_id = (
        pending_order_store.create_order_intent(
            symbol=symbol,
            exchange="NSE",
            action="EXIT",
            side="SELL",
            requested_quantity=quantity,
        )
    )

    pending_order_store.attach_broker_order_id(
        operation_id,
        order_id,
    )

    return operation_id


def run_partial_to_complete_test(tmp):
    trades = []
    saved_positions = []

    original_record_trade = main_module.record_trade
    original_save_positions = main_module.save_positions

    try:
        main_module.record_trade = (
            lambda *args, **kwargs:
            trades.append(
                {
                    "args": args,
                    "kwargs": kwargs,
                }
            )
        )

        def capture_save(
            positions,
            positions_path=None,
        ):
            saved_positions.append(
                deepcopy(positions)
            )

        main_module.save_positions = capture_save

        operation_id = create_submitted_exit(
            "RECOVER",
            10,
            "EXIT-RECOVER-1",
        )

        # Before the crash, 3/10 shares had already been confirmed,
        # applied to the position and accounted for.
        initial_result = execution_result(
            "EXIT-RECOVER-1",
            "TIMEOUT",
            10,
            3,
            7,
            0,
            108.0,
            False,
        )

        pending_order_store.update_order_verification(
            operation_id,
            initial_result,
        )

        positions = {
            "RECOVER": {
                "direction": "BUY",
                "qty": 7,
                "entry": 100.0,
                "stop": 95.0,
                "target": 105.0,
                "exchange": "NSE",
                "peak_price": 110.0,
                "tight_mode": False,
                "entry_time": None,
                "exit_reason": "fixed_target",
                "exit_order_id": "EXIT-RECOVER-1",
                "exit_operation_id": operation_id,
                "exit_requested_quantity": 10,
                "exit_filled_quantity": 3,
                "exit_average_price": 108.0,
                "exit_confirmation_pending": True,
            }
        }

        risk = FakeRisk()

        # Broker cumulative fill advances from 3 to 6 shares.
        # Cumulative average moves from 108 to 109:
        #
        # incremental price =
        #   (6*109 - 3*108) / 3 = 110
        kite = FakeKite(
            {
                "status": "OPEN",
                "filled_quantity": 6,
                "pending_quantity": 4,
                "cancelled_quantity": 0,
                "average_price": 109.0,
                "status_message": None,
                "exchange_order_id": "EX-1",
            }
        )

        main_module.recover_unresolved_exits(
            kite,
            positions,
            risk,
            cfg(),
            positions_path=os.path.join(
                tmp,
                "positions.json",
            ),
        )

        check(
            "1. Recovery applies only 3 newly confirmed shares",
            positions["RECOVER"]["qty"] == 4,
        )

        check(
            "1b. Local cumulative applied quantity becomes 6",
            positions["RECOVER"][
                "exit_filled_quantity"
            ] == 6,
        )

        check(
            "1c. Recovery records exactly one incremental trade",
            len(trades) == 1,
        )

        check(
            "1d. Incremental trade quantity is 3",
            trades[0]["args"][2] == 3,
        )

        check(
            "1e. Incremental execution price is derived as 110",
            abs(trades[0]["args"][4] - 110.0)
            < 0.000001,
        )

        check(
            "1f. Non-terminal partial exit stays unresolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is False,
        )

        # Running recovery again with exactly the same cumulative
        # broker fill must apply nothing.
        main_module.recover_unresolved_exits(
            kite,
            positions,
            risk,
            cfg(),
            positions_path=os.path.join(
                tmp,
                "positions.json",
            ),
        )

        check(
            "2. Repeated recovery does not reduce quantity again",
            positions["RECOVER"]["qty"] == 4,
        )

        check(
            "2b. Repeated recovery does not duplicate trade P&L",
            len(trades) == 1,
        )

        # Broker later completes all 10 shares at cumulative avg 110.
        #
        # Previous cumulative notional = 6*109 = 654
        # Final cumulative notional    = 10*110 = 1100
        # New 4-share notional         = 446
        # Incremental price            = 446/4 = 111.5
        kite.record = {
            "status": "COMPLETE",
            "filled_quantity": 10,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 110.0,
            "status_message": None,
            "exchange_order_id": "EX-1",
        }

        main_module.recover_unresolved_exits(
            kite,
            positions,
            risk,
            cfg(),
            positions_path=os.path.join(
                tmp,
                "positions.json",
            ),
        )

        check(
            "3. Final recovery removes fully closed position",
            "RECOVER" not in positions,
        )

        check(
            "3b. Final recovery records one additional trade",
            len(trades) == 2,
        )

        check(
            "3c. Final incremental trade quantity is 4",
            trades[1]["args"][2] == 4,
        )

        check(
            "3d. Final incremental execution price is 111.5",
            abs(trades[1]["args"][4] - 111.5)
            < 0.000001,
        )

        check(
            "3e. Terminal completed exit is resolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is True,
        )

        check(
            "3f. Risk receives P&L only twice, once per new fill",
            len(risk.results) == 2,
        )

    finally:
        main_module.record_trade = original_record_trade
        main_module.save_positions = original_save_positions


def run_rejected_test(tmp):
    operation_id = create_submitted_exit(
        "REJECTREC",
        5,
        "EXIT-REJECT-1",
    )

    positions = {
        "REJECTREC": {
            "direction": "BUY",
            "qty": 5,
            "entry": 100.0,
            "stop": 95.0,
            "target": 105.0,
            "exchange": "NSE",
            "exit_reason": "stop",
        }
    }

    risk = FakeRisk()

    original_save_positions = (
        main_module.save_positions
    )

    try:
        main_module.save_positions = (
            lambda *args, **kwargs: None
        )

        kite = FakeKite(
            {
                "status": "REJECTED",
                "filled_quantity": 0,
                "pending_quantity": 0,
                "cancelled_quantity": 5,
                "average_price": 0,
                "status_message": "broker rejected",
                "exchange_order_id": None,
            }
        )

        main_module.recover_unresolved_exits(
            kite,
            positions,
            risk,
            cfg(),
            positions_path=os.path.join(
                tmp,
                "rejected_positions.json",
            ),
        )

        check(
            "4. Rejected recovered exit leaves quantity unchanged",
            positions["REJECTREC"]["qty"] == 5,
        )

        check(
            "4b. Rejected recovered exit records no P&L",
            len(risk.results) == 0,
        )

        check(
            "4c. Rejected terminal operation is resolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is True,
        )

    finally:
        main_module.save_positions = (
            original_save_positions
        )


def run_no_order_id_test(tmp):
    operation_id = (
        pending_order_store.create_order_intent(
            symbol="NOORDERID",
            exchange="NSE",
            action="EXIT",
            side="SELL",
            requested_quantity=3,
        )
    )

    positions = {
        "NOORDERID": {
            "direction": "BUY",
            "qty": 3,
            "entry": 100.0,
            "stop": 95.0,
            "target": 105.0,
            "exchange": "NSE",
        }
    }

    risk = FakeRisk()

    kite = FakeKite(
        {
            "status": "COMPLETE",
            "filled_quantity": 3,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 105.0,
        }
    )

    main_module.recover_unresolved_exits(
        kite,
        positions,
        risk,
        cfg(),
        positions_path=os.path.join(
            tmp,
            "no_order_positions.json",
        ),
    )

    check(
        "5. Missing broker order ID makes no history call",
        kite.history_calls == 0,
    )

    check(
        "5b. Missing order ID leaves position unchanged",
        positions["NOORDERID"]["qty"] == 3,
    )

    check(
        "5c. Missing order ID remains unresolved",
        pending_order_store.get_order(
            operation_id
        )["resolved"] is False,
    )


print("--- Stage 4 EXIT Restart Recovery ---")

original_store_path = pending_order_store.STORE_PATH

try:
    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )

        run_partial_to_complete_test(tmp)

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )

        run_rejected_test(tmp)

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )

        run_no_order_id_test(tmp)

finally:
    pending_order_store.STORE_PATH = (
        original_store_path
    )

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
