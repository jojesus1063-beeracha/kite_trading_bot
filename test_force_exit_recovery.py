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
        exchange_order_id="EXCHANGE-FORCE",
        terminal=terminal,
        verified_at=datetime.now(),
        history_attempts=1,
        api_error_count=0,
    )


def create_force_exit(
    symbol,
    quantity,
    order_id=None,
):
    operation_id = (
        pending_order_store.create_order_intent(
            symbol=symbol,
            exchange="NSE",
            action="FORCE_EXIT",
            side="SELL",
            requested_quantity=quantity,
        )
    )

    if order_id is not None:
        pending_order_store.attach_broker_order_id(
            operation_id,
            order_id,
        )

    return operation_id


def test_partial_to_complete(tmp):
    trades = []
    saves = []

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

        main_module.save_positions = (
            lambda positions, *args, **kwargs:
            saves.append(deepcopy(positions))
        )

        operation_id = create_force_exit(
            "FORCERECOVER",
            10,
            "FORCE-RECOVER-1",
        )

        # Three shares were already confirmed and applied before
        # the simulated crash.
        initial = execution_result(
            "FORCE-RECOVER-1",
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
            initial,
        )

        positions = {
            "FORCERECOVER": {
                "direction": "BUY",
                "qty": 7,
                "entry": 100.0,
                "stop": 95.0,
                "target": 105.0,
                "exchange": "NSE",
                "force_exit_reason": "square_off",
                "force_exit_order_id": "FORCE-RECOVER-1",
                "force_exit_operation_id": operation_id,
                "force_exit_requested_quantity": 10,
                "force_exit_filled_quantity": 3,
                "force_exit_average_price": 108.0,
                "force_exit_confirmation_pending": True,
            }
        }

        risk = FakeRisk()

        # Cumulative fill advances from 3 to 6 at average 109.
        # Incremental price = (6*109 - 3*108) / 3 = 110.
        kite = FakeKite(
            {
                "status": "OPEN",
                "filled_quantity": 6,
                "pending_quantity": 4,
                "cancelled_quantity": 0,
                "average_price": 109.0,
                "status_message": None,
                "exchange_order_id": "EX-FORCE-1",
            }
        )

        main_module.recover_unresolved_force_exits(
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
            positions["FORCERECOVER"]["qty"] == 4,
        )
        check(
            "1b. Cumulative applied force quantity becomes 6",
            positions["FORCERECOVER"][
                "force_exit_filled_quantity"
            ] == 6,
        )
        check(
            "1c. One incremental trade is recorded",
            len(trades) == 1,
        )
        check(
            "1d. Incremental trade quantity is 3",
            trades[0]["args"][2] == 3,
        )
        check(
            "1e. Incremental force-exit price is 110",
            abs(trades[0]["args"][4] - 110.0)
            < 0.000001,
        )
        check(
            "1f. Non-terminal FORCE_EXIT remains unresolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is False,
        )

        # The same cumulative broker state must apply nothing twice.
        main_module.recover_unresolved_force_exits(
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
            "2. Repeated recovery does not reduce quantity twice",
            positions["FORCERECOVER"]["qty"] == 4,
        )
        check(
            "2b. Repeated recovery does not duplicate P&L",
            len(trades) == 1,
        )

        # Broker completes all 10 at cumulative average 110.
        # New 4-share price:
        # (10*110 - 6*109) / 4 = 111.5
        kite.record = {
            "status": "COMPLETE",
            "filled_quantity": 10,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 110.0,
            "status_message": None,
            "exchange_order_id": "EX-FORCE-1",
        }

        main_module.recover_unresolved_force_exits(
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
            "FORCERECOVER" not in positions,
        )
        check(
            "3b. Final recovery records one additional trade",
            len(trades) == 2,
        )
        check(
            "3c. Final trade quantity is 4",
            trades[1]["args"][2] == 4,
        )
        check(
            "3d. Final incremental price is 111.5",
            abs(trades[1]["args"][4] - 111.5)
            < 0.000001,
        )
        check(
            "3e. Completed FORCE_EXIT is resolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is True,
        )
        check(
            "3f. Risk records P&L only for two new-fill events",
            len(risk.results) == 2,
        )

    finally:
        main_module.record_trade = original_record_trade
        main_module.save_positions = original_save_positions


def test_rejected(tmp):
    operation_id = create_force_exit(
        "FORCEREJECT",
        5,
        "FORCE-REJECT-1",
    )

    positions = {
        "FORCEREJECT": {
            "direction": "BUY",
            "qty": 5,
            "entry": 100.0,
            "stop": 95.0,
            "target": 105.0,
            "exchange": "NSE",
        }
    }

    risk = FakeRisk()
    original_save = main_module.save_positions

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

        main_module.recover_unresolved_force_exits(
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
            "4. Rejected recovery leaves position unchanged",
            positions["FORCEREJECT"]["qty"] == 5,
        )
        check(
            "4b. Rejected recovery records no P&L",
            len(risk.results) == 0,
        )
        check(
            "4c. Rejected terminal FORCE_EXIT is resolved",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is True,
        )

    finally:
        main_module.save_positions = original_save


def test_no_order_id(tmp):
    operation_id = create_force_exit(
        "FORCENOID",
        3,
        None,
    )

    positions = {
        "FORCENOID": {
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

    main_module.recover_unresolved_force_exits(
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
        "5. Missing broker order ID makes no history call",
        kite.history_calls == 0,
    )
    check(
        "5b. Missing order ID leaves position unchanged",
        positions["FORCENOID"]["qty"] == 3,
    )
    check(
        "5c. Missing order ID remains unresolved",
        pending_order_store.get_order(
            operation_id
        )["resolved"] is False,
    )


def test_missing_position_full_terminal(tmp):
    operation_id = create_force_exit(
        "FORCEABSENT",
        4,
        "FORCE-ABSENT-1",
    )

    positions = {}
    risk = FakeRisk()

    kite = FakeKite(
        {
            "status": "COMPLETE",
            "filled_quantity": 4,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 107.0,
            "status_message": None,
            "exchange_order_id": "EX-ABSENT",
        }
    )

    original_record_trade = main_module.record_trade
    trades = []

    try:
        main_module.record_trade = (
            lambda *args, **kwargs:
            trades.append(args)
        )

        main_module.recover_unresolved_force_exits(
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
            "6. Already-absent full close records no duplicate trade",
            len(trades) == 0,
        )
        check(
            "6b. Already-absent full terminal operation resolves",
            pending_order_store.get_order(
                operation_id
            )["resolved"] is True,
        )
        check(
            "6c. No position is fabricated",
            positions == {},
        )

    finally:
        main_module.record_trade = original_record_trade


print("--- Stage 5 FORCE_EXIT Restart Recovery ---")

original_store = pending_order_store.STORE_PATH

try:
    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )
        test_partial_to_complete(tmp)

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )
        test_rejected(tmp)

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )
        test_no_order_id(tmp)

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )
        test_missing_position_full_terminal(tmp)

finally:
    pending_order_store.STORE_PATH = original_store

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
