"""Focused, broker-free tests for confirmed-entry protection integration."""

import contextlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import executor
import pending_order_store
from data_feed import (
    get_cached_instrument_tick_size,
    get_instrument_token,
)
from entry_protection import (
    apply_protective_stop_result,
    build_confirmed_position,
    build_entry_plan,
    needs_initial_stop_recovery,
    protect_confirmed_position,
)
from protective_stop import recover_protective_stop
from protective_stop_store import (
    create_protective_stop_intent,
    get_protective_stop,
)


@contextlib.contextmanager
def isolated_pending_store():
    with tempfile.TemporaryDirectory() as directory:
        original = pending_order_store.STORE_PATH
        path = os.path.join(directory, "pending.json")
        pending_order_store.STORE_PATH = path
        try:
            yield path
        finally:
            pending_order_store.STORE_PATH = original


def entry_cfg(*, paper=False):
    return SimpleNamespace(
        PAPER_TRADING=paper,
        CHECK_MARGIN_BEFORE_ENTRY=False,
        CIRCUIT_PROXIMITY_PCT=None,
        VARIETY="regular",
        PRODUCT="MIS",
        ORDER_TYPE_ENTRY="MARKET",
        MARKET_PROTECTION=-1,
        ORDER_VERIFY_MAX_WAIT_SECONDS=0,
        ORDER_VERIFY_POLL_INTERVAL_SECONDS=0,
        ENTRY_RECONCILE_MAX_WAIT_SECONDS=0,
        ENTRY_RECONCILE_POLL_INTERVAL_SECONDS=0,
        ENABLE_FIXED_TARGET=True,
        STOP_LOSS_PERCENT=0.45,
        PROFIT_TARGET_PERCENT=1.5,
        PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS=0,
        PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS=0,
        PROTECTIVE_STOP_RECONCILE_MAX_WAIT_SECONDS=0,
        PROTECTIVE_STOP_RECONCILE_POLL_INTERVAL_SECONDS=0,
    )


def signal():
    return SimpleNamespace(
        direction="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        target=102.0,
        timestamp="2026-08-04T10:00:00+05:30",
    )


def confirmed_result(*, filled=10, average=101.0):
    return {
        "success": True,
        "order_id": "ENTRY-1",
        "operation_id": "ENTRY-OP-1",
        "client_tag": "KBE12345678901234567",
        "status": "COMPLETE",
        "reason": None,
        "requested_quantity": 10,
        "filled_quantity": filled,
        "average_price": average,
        "entry_confirmation_pending": False,
    }


def test_confirmed_fill_builds_levels_from_broker_average():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(average=101.0),
        "NSE",
        cfg,
    )

    assert position["entry"] == 101.0
    assert round(position["stop"], 6) == round(101.0 * 0.9955, 6)
    assert round(position["target"], 6) == round(101.0 * 1.015, 6)
    assert position["protective_stop_state"] == "PENDING"
    assert position["automated_exit_blocked"] is True


def test_startup_instrument_lookup_captures_broker_tick_size():
    kite = MagicMock()
    kite.instruments.return_value = [{
        "tradingsymbol": "INFY",
        "instrument_token": 123,
        "tick_size": 0.01,
    }]

    assert get_instrument_token(kite, "INFY", "NSE") == 123
    assert get_cached_instrument_tick_size("INFY", "NSE") == 0.01


def test_full_fill_places_one_stop_for_confirmed_quantity():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(filled=10),
        "NSE",
        cfg,
    )
    calls = []

    def fake_stop_placer(_kite, **kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "active": True,
            "triggered": False,
            "confirmation_pending": False,
            "state": "ACTIVE",
            "status": "TRIGGER PENDING",
            "reason": None,
            "operation_id": "STOP-OP-1",
            "order_id": "STOP-1",
            "client_tag": "KBS12345678901234567",
            "trigger_price": 100.55,
            "requested_quantity": 10,
        }

    result = protect_confirmed_position(
        MagicMock(),
        "INFY",
        position,
        cfg,
        stop_placer=fake_stop_placer,
    )

    assert result["active"] is True
    assert len(calls) == 1
    assert calls[0]["quantity"] == 10
    assert calls[0]["confirmed_entry_price"] == 101.0
    assert position["entry_protected"] is True
    assert position["protective_stop_quantity"] == 10
    assert position["protective_stop_order_id"] == "STOP-1"


def test_partial_fill_protects_only_confirmed_quantity():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(filled=4),
        "NSE",
        cfg,
    )
    calls = []

    def fake_stop_placer(_kite, **kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "active": True,
            "triggered": False,
            "confirmation_pending": False,
            "state": "ACTIVE",
            "status": "TRIGGER PENDING",
            "reason": None,
            "operation_id": "STOP-OP-PART",
            "order_id": "STOP-PART",
            "client_tag": "KBS12345678901234567",
            "trigger_price": 100.55,
            "requested_quantity": 4,
        }

    protect_confirmed_position(
        MagicMock(),
        "INFY",
        position,
        cfg,
        stop_placer=fake_stop_placer,
    )

    assert position["qty"] == 4
    assert calls[0]["quantity"] == 4
    assert position["protective_stop_quantity"] == 4


def test_uncertain_stop_is_not_treated_as_protected():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(),
        "NSE",
        cfg,
    )

    def uncertain_stop(_kite, **_kwargs):
        return {
            "success": False,
            "active": False,
            "triggered": False,
            "confirmation_pending": True,
            "state": "SUBMISSION_UNCERTAIN",
            "status": "SUBMISSION_UNCERTAIN",
            "reason": "response lost",
            "operation_id": "STOP-OP-U",
            "order_id": None,
            "client_tag": "KBS12345678901234567",
            "trigger_price": 100.55,
            "requested_quantity": 10,
        }

    protect_confirmed_position(
        MagicMock(),
        "INFY",
        position,
        cfg,
        stop_placer=uncertain_stop,
    )

    assert position["entry_protected"] is False
    assert position["manual_reconciliation_required"] is True
    assert position["protective_stop_state"] == "SUBMISSION_UNCERTAIN"
    assert position["automated_exit_blocked"] is True


def test_active_stop_with_insufficient_quantity_remains_blocked():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(filled=10),
        "NSE",
        cfg,
    )
    result = {
        "success": True,
        "active": True,
        "triggered": False,
        "confirmation_pending": False,
        "state": "ACTIVE",
        "status": "TRIGGER PENDING",
        "reason": None,
        "operation_id": "STOP-OP-PARTIAL-COVER",
        "order_id": "STOP-PARTIAL-COVER",
        "client_tag": "KBS12345678901234567",
        "trigger_price": 100.55,
        "requested_quantity": 4,
    }

    apply_protective_stop_result(position, result)

    assert position["protective_stop_active"] is True
    assert position["entry_protected"] is False
    assert position["protective_stop_state"] == (
        "PROTECTION_QUANTITY_MISMATCH"
    )
    assert position["manual_reconciliation_required"] is True


def test_main_persists_pending_exposure_before_stop_side_effect():
    source = Path("main.py").read_text(encoding="utf-8")
    start = source.index("position = build_confirmed_position(")
    first_save = source.index("save_positions(open_positions)", start)
    stop_call = source.index("protect_confirmed_position(", first_save)
    second_save = source.index("save_positions(open_positions)", stop_call)

    assert start < first_save < stop_call < second_save


def test_only_pre_intent_pending_state_can_create_stop_on_restart():
    cfg = entry_cfg()
    position = build_confirmed_position(
        signal(),
        confirmed_result(),
        "NSE",
        cfg,
    )

    assert needs_initial_stop_recovery(
        position,
        has_store_record=False,
    ) is True
    assert needs_initial_stop_recovery(
        position,
        has_store_record=True,
    ) is False

    position["protective_stop_state"] = "SUBMISSION_UNCERTAIN"
    assert needs_initial_stop_recovery(
        position,
        has_store_record=False,
    ) is False


def test_paper_mode_never_calls_stop_placer():
    cfg = entry_cfg(paper=True)
    position = build_confirmed_position(
        signal(),
        confirmed_result(average=None),
        "NSE",
        cfg,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paper mode attempted a broker stop")

    result = protect_confirmed_position(
        MagicMock(),
        "INFY",
        position,
        cfg,
        stop_placer=forbidden,
    )

    assert result["paper"] is True
    assert position["protective_stop_state"] == "PAPER"
    assert position["automated_exit_blocked"] is False


class LostResponseEntryKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, *, match_count=1):
        self.match_count = match_count
        self.place_calls = []
        self.history_calls = []

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        raise RuntimeError("response lost after broker acceptance")

    def orders(self):
        submitted = self.place_calls[-1]
        return [
            {
                "order_id": f"RECOVERED-{index}",
                "tag": submitted["tag"],
                "tradingsymbol": submitted["tradingsymbol"],
                "exchange": submitted["exchange"],
                "transaction_type": submitted["transaction_type"],
                "quantity": submitted["quantity"],
                "product": submitted["product"],
                "order_type": submitted["order_type"],
            }
            for index in range(self.match_count)
        ]

    def order_history(self, order_id):
        self.history_calls.append(order_id)
        return [{
            "status": "COMPLETE",
            "filled_quantity": 10,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 101.0,
            "status_message": None,
            "exchange_order_id": "EX-ENTRY-1",
        }]


def test_lost_entry_response_recovers_unique_order_without_resubmit():
    cfg = entry_cfg()
    kite = LostResponseEntryKite(match_count=1)

    with isolated_pending_store():
        result = executor.place_entry_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg,
            entry_plan=build_entry_plan(signal(), cfg),
        )

    assert result["success"] is True
    assert result["order_id"] == "RECOVERED-0"
    assert len(kite.place_calls) == 1
    assert kite.history_calls == ["RECOVERED-0"]


def test_ambiguous_entry_response_never_resubmits():
    cfg = entry_cfg()
    kite = LostResponseEntryKite(match_count=2)

    with isolated_pending_store():
        result = executor.place_entry_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg,
        )

    assert result["success"] is False
    assert result["status"] == "SUBMISSION_AMBIGUOUS"
    assert result["entry_confirmation_pending"] is True
    assert len(kite.place_calls) == 1
    assert kite.history_calls == []


def test_confirmed_fill_remains_recoverable_until_locally_applied():
    with isolated_pending_store() as path:
        operation_id = pending_order_store.create_order_intent(
            "INFY",
            "NSE",
            "ENTRY",
            "BUY",
            10,
            path=path,
            client_tag="KBE12345678901234567",
        )
        pending_order_store.attach_broker_order_id(
            operation_id,
            "ENTRY-1",
            path=path,
        )
        verification = SimpleNamespace(
            filled_quantity=10,
            pending_quantity=0,
            cancelled_quantity=0,
            average_price=101.0,
            status="COMPLETE",
            terminal=True,
            status_message=None,
            exchange_order_id="EX-1",
            history_attempts=1,
            api_error_count=0,
        )
        pending_order_store.update_order_verification(
            operation_id,
            verification,
            path=path,
        )
        pending_order_store.mark_order_resolved(
            operation_id,
            path=path,
        )

        requiring_application = (
            pending_order_store.list_entry_orders_requiring_local_application(
                path
            )
        )
        assert [item["operation_id"] for item in requiring_application] == [
            operation_id
        ]

        pending_order_store.mark_entry_fill_applied(
            operation_id,
            10,
            path=path,
        )
        assert not pending_order_store.list_entry_orders_requiring_local_application(
            path
        )


class StopRecoveryKite:
    def __init__(self, broker_order):
        self.broker_order = broker_order
        self.place_calls = 0

    def orders(self):
        return [self.broker_order]

    def order_history(self, _order_id):
        return [{
            "status": "TRIGGER PENDING",
            "filled_quantity": 0,
            "pending_quantity": 10,
            "cancelled_quantity": 0,
            "average_price": 0,
            "status_message": None,
            "exchange_order_id": "EX-STOP-1",
        }]

    def place_order(self, **_kwargs):
        self.place_calls += 1
        raise AssertionError("recovery must never submit another stop")


def test_restart_recovers_stop_by_tag_without_place_order():
    cfg = entry_cfg()

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        operation_id = create_protective_stop_intent(
            symbol="INFY",
            exchange="NSE",
            position_direction="BUY",
            stop_side="SELL",
            requested_quantity=10,
            trigger_price=100.55,
            client_tag="KBS12345678901234567",
            tick_size=0.05,
            entry_operation_id="ENTRY-OP-1",
            path=path,
        )
        broker_order = {
            "order_id": "RECOVERED-STOP-1",
            "tag": "KBS12345678901234567",
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "product": "MIS",
            "quantity": 10,
            "trigger_price": 100.55,
        }
        kite = StopRecoveryKite(broker_order)
        record = get_protective_stop(operation_id, path=path)

        result = recover_protective_stop(
            kite,
            record,
            cfg,
            store_path=path,
        )

        assert result["active"] is True
        assert result["order_id"] == "RECOVERED-STOP-1"
        assert result["submission_reconciled"] is True
        assert kite.place_calls == 0


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"ENTRY PROTECTION INTEGRATION TESTS PASSED ({len(tests)})")
