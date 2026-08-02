import os
import tempfile
from copy import deepcopy
from types import SimpleNamespace

import main as main_module
import pending_order_store

from executor import place_force_exit_order


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
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(
        self,
        history_record=None,
        place_exception=None,
        order_id="FORCE-ORDER-1",
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

        return [deepcopy(self.history_record)]


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


def make_position(quantity=5):
    return {
        "direction": "BUY",
        "qty": quantity,
        "entry": 100.0,
        "stop": 95.0,
        "target": 105.0,
        "exchange": "NSE",
        "peak_price": 106.0,
        "tight_mode": False,
        "entry_time": None,
    }


def run_isolated(test_fn):
    original_store = pending_order_store.STORE_PATH
    original_record_trade = main_module.record_trade
    original_save_positions = main_module.save_positions
    original_paper = main_module.cfg.PAPER_TRADING

    with tempfile.TemporaryDirectory() as tmp:
        pending_order_store.STORE_PATH = os.path.join(
            tmp,
            "pending_orders.json",
        )

        trades = []
        saves = []

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

        try:
            test_fn(tmp, trades, saves)
        finally:
            pending_order_store.STORE_PATH = (
                original_store
            )
            main_module.record_trade = (
                original_record_trade
            )
            main_module.save_positions = (
                original_save_positions
            )
            main_module.cfg.PAPER_TRADING = (
                original_paper
            )


def test_complete_force_exit(tmp, trades, saves):
    main_module.cfg.PAPER_TRADING = False

    kite = FakeKite(
        {
            "status": "COMPLETE",
            "filled_quantity": 5,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 106.25,
            "status_message": None,
            "exchange_order_id": "EX-FORCE-1",
        }
    )

    result = place_force_exit_order(
        kite,
        "FORCEFULL",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    stored_before = pending_order_store.get_order(
        result["operation_id"]
    )

    check(
        "1. FORCE_EXIT action persisted",
        stored_before["action"] == "FORCE_EXIT",
    )

    check(
        "1b. Broker-complete force exit is not resolved "
        "before local application",
        stored_before["resolved"] is False,
    )

    positions = {
        "FORCEFULL": make_position(5)
    }
    risk = FakeRisk()

    status = main_module.apply_force_exit_result(
        "FORCEFULL",
        positions["FORCEFULL"],
        result,
        positions,
        risk,
        "NSE",
        105.0,
        positions_path=os.path.join(
            tmp,
            "positions.json",
        ),
    )

    check(
        "1c. Full confirmed force exit removes position",
        "FORCEFULL" not in positions,
    )
    check(
        "1d. One confirmed trade is recorded",
        len(trades) == 1,
    )
    check(
        "1e. Recorded quantity is 5",
        trades[0]["args"][2] == 5,
    )
    check(
        "1f. Broker average price is used",
        trades[0]["args"][4] == 106.25,
    )
    check(
        "1g. Risk receives confirmed P&L",
        len(risk.results) == 1,
    )
    check(
        "1h. Durable FORCE_EXIT resolves after application",
        pending_order_store.get_order(
            result["operation_id"]
        )["resolved"] is True,
    )
    check(
        "1i. Status identifies full force close",
        "FORCE CLOSED" in status,
    )


def test_partial_timeout(tmp, trades, saves):
    main_module.cfg.PAPER_TRADING = False

    kite = FakeKite(
        {
            "status": "OPEN",
            "filled_quantity": 2,
            "pending_quantity": 3,
            "cancelled_quantity": 0,
            "average_price": 105.50,
            "status_message": None,
            "exchange_order_id": "EX-FORCE-2",
        },
        order_id="FORCE-ORDER-2",
    )

    result = place_force_exit_order(
        kite,
        "FORCEPART",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    positions = {
        "FORCEPART": make_position(5)
    }
    risk = FakeRisk()

    status = main_module.apply_force_exit_result(
        "FORCEPART",
        positions["FORCEPART"],
        result,
        positions,
        risk,
        "NSE",
        105.0,
        positions_path=os.path.join(
            tmp,
            "positions.json",
        ),
    )

    check(
        "2. Partial force exit keeps position",
        "FORCEPART" in positions,
    )
    check(
        "2b. Only confirmed 2 shares are removed",
        positions["FORCEPART"]["qty"] == 3,
    )
    check(
        "2c. P&L recorded for only 2 shares",
        trades[0]["args"][2] == 2,
    )
    check(
        "2d. Pending state is persisted",
        positions["FORCEPART"][
            "force_exit_confirmation_pending"
        ] is True,
    )
    check(
        "2e. Non-terminal operation stays unresolved",
        pending_order_store.get_order(
            result["operation_id"]
        )["resolved"] is False,
    )
    check(
        "2f. Status identifies partial force exit",
        "FORCE PARTIAL EXIT" in status,
    )


def test_zero_fill_timeout(tmp, trades, saves):
    main_module.cfg.PAPER_TRADING = False

    kite = FakeKite(
        {
            "status": "OPEN",
            "filled_quantity": 0,
            "pending_quantity": 5,
            "cancelled_quantity": 0,
            "average_price": 0,
            "status_message": None,
            "exchange_order_id": "EX-FORCE-3",
        },
        order_id="FORCE-ORDER-3",
    )

    result = place_force_exit_order(
        kite,
        "FORCEZERO",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    positions = {
        "FORCEZERO": make_position(5)
    }
    risk = FakeRisk()

    status = main_module.apply_force_exit_result(
        "FORCEZERO",
        positions["FORCEZERO"],
        result,
        positions,
        risk,
        "NSE",
        105.0,
        positions_path=os.path.join(
            tmp,
            "positions.json",
        ),
    )

    check(
        "3. Zero-fill force exit retains all shares",
        positions["FORCEZERO"]["qty"] == 5,
    )
    check(
        "3b. Zero-fill force exit records no trade",
        len(trades) == 0,
    )
    check(
        "3c. Zero-fill force exit records no P&L",
        len(risk.results) == 0,
    )
    check(
        "3d. Zero-fill operation remains unresolved",
        pending_order_store.get_order(
            result["operation_id"]
        )["resolved"] is False,
    )
    check(
        "3e. Status identifies pending force exit",
        "FORCE EXIT PENDING" in status,
    )


def test_exit_family_duplicate_lock(tmp, trades, saves):
    operation_id = (
        pending_order_store.create_order_intent(
            symbol="LOCKED",
            exchange="NSE",
            action="EXIT",
            side="SELL",
            requested_quantity=5,
        )
    )

    kite = FakeKite()

    result = place_force_exit_order(
        kite,
        "LOCKED",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    check(
        "4. Existing normal EXIT blocks FORCE_EXIT",
        result["status"]
        == "FORCE_EXIT_BLOCKED_PENDING_ORDER",
    )
    check(
        "4b. No duplicate broker order is submitted",
        kite.place_calls == 0,
    )
    check(
        "4c. Original normal EXIT remains unresolved",
        pending_order_store.get_order(
            operation_id
        )["resolved"] is False,
    )


def test_submission_uncertain(tmp, trades, saves):
    kite = FakeKite(
        place_exception=TimeoutError(
            "network timeout during force submission"
        )
    )

    first = place_force_exit_order(
        kite,
        "FORCEUNCERTAIN",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    second = place_force_exit_order(
        kite,
        "FORCEUNCERTAIN",
        "BUY",
        5,
        "NSE",
        live_cfg(),
    )

    check(
        "5. Submission exception is uncertain",
        first["status"] == "SUBMISSION_UNCERTAIN",
    )
    check(
        "5b. Uncertain operation remains unresolved",
        first["resolved"] is False,
    )
    check(
        "5c. Blind retry is blocked",
        second["status"]
        == "FORCE_EXIT_BLOCKED_PENDING_ORDER",
    )
    check(
        "5d. Broker submission attempted once",
        kite.place_calls == 1,
    )


def test_paper_force_exit(tmp, trades, saves):
    main_module.cfg.PAPER_TRADING = True

    kite = FakeKite()

    result = place_force_exit_order(
        kite,
        "FORCEPAPER",
        "BUY",
        4,
        "NSE",
        paper_cfg(),
    )

    positions = {
        "FORCEPAPER": make_position(4)
    }
    risk = FakeRisk()

    main_module.apply_force_exit_result(
        "FORCEPAPER",
        positions["FORCEPAPER"],
        result,
        positions,
        risk,
        "NSE",
        104.25,
        positions_path=os.path.join(
            tmp,
            "positions.json",
        ),
    )

    check(
        "6. Paper force exit makes no broker submission",
        kite.place_calls == 0,
    )
    check(
        "6b. Paper force exit makes no history call",
        kite.history_calls == 0,
    )
    check(
        "6c. Paper force exit removes synthetic position",
        "FORCEPAPER" not in positions,
    )
    check(
        "6d. Paper mode uses supplied market fallback price",
        trades[0]["args"][4] == 104.25,
    )


print("--- Stage 5 Verified FORCE_EXIT ---")

run_isolated(test_complete_force_exit)
run_isolated(test_partial_timeout)
run_isolated(test_zero_fill_timeout)
run_isolated(test_exit_family_duplicate_lock)
run_isolated(test_submission_uncertain)
run_isolated(test_paper_force_exit)

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
