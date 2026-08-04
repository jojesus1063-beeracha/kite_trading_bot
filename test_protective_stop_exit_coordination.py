"""Broker-free tests for protective-stop/market-exit coordination."""

import contextlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pending_order_store
import protective_stop_store
import main as main_module
from executor import place_exit_order, place_force_exit_order
from protective_stop_exit import coordinate_protective_stop_for_exit
from protective_stop_store import (
    attach_protective_stop_order_id,
    create_protective_stop_intent,
    get_protective_stop,
    mark_protective_stop_fill_applied,
)


def cfg(*, paper=False):
    return SimpleNamespace(
        PAPER_TRADING=paper,
        VARIETY="regular",
        PRODUCT="MIS",
        MARKET_PROTECTION=-1,
        PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS=0,
        PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS=0,
        ORDER_VERIFY_MAX_WAIT_SECONDS=0,
        ORDER_VERIFY_POLL_INTERVAL_SECONDS=0,
    )


def active(quantity=10):
    return [{
        "status": "TRIGGER PENDING",
        "filled_quantity": 0,
        "pending_quantity": quantity,
        "cancelled_quantity": 0,
        "average_price": 0,
        "status_message": None,
        "exchange_order_id": "EX-STOP",
    }]


def cancelled(quantity=10, *, filled=0, average=0):
    return [{
        "status": "CANCELLED",
        "filled_quantity": filled,
        "pending_quantity": 0,
        "cancelled_quantity": quantity - filled,
        "average_price": average,
        "status_message": None,
        "exchange_order_id": "EX-STOP",
    }]


def complete(quantity=10, *, average=99.5):
    return [{
        "status": "COMPLETE",
        "filled_quantity": quantity,
        "pending_quantity": 0,
        "cancelled_quantity": 0,
        "average_price": average,
        "status_message": None,
        "exchange_order_id": "EX-STOP",
    }]


def exit_complete(quantity=10, *, average=101.5):
    return [{
        "status": "COMPLETE",
        "filled_quantity": quantity,
        "pending_quantity": 0,
        "cancelled_quantity": 0,
        "average_price": average,
        "status_message": None,
        "exchange_order_id": "EX-EXIT",
    }]


class FakeKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(
        self,
        histories,
        *,
        cancel_error=None,
        before_cancel=None,
    ):
        self.histories = list(histories)
        self.cancel_error = cancel_error
        self.before_cancel = before_cancel
        self.history_calls = 0
        self.cancel_calls = []
        self.place_calls = []

    def order_history(self, order_id):
        index = min(self.history_calls, len(self.histories) - 1)
        self.history_calls += 1
        return self.histories[index]

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)

        if self.before_cancel:
            self.before_cancel()

        if self.cancel_error is not None:
            raise self.cancel_error

        return kwargs["order_id"]

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return "EXIT-1"


class FakeRisk:
    def __init__(self):
        self.results = []

    def record_trade_result(self, pnl):
        self.results.append(pnl)


def position(quantity=10):
    return {
        "direction": "BUY",
        "qty": quantity,
        "entry": 100.0,
        "exchange": "NSE",
        "protective_stop_operation_id": None,
    }


def create_stop(path, pos, quantity=10):
    operation_id = create_protective_stop_intent(
        symbol="INFY",
        exchange="NSE",
        position_direction="BUY",
        stop_side="SELL",
        requested_quantity=quantity,
        trigger_price=99.55,
        client_tag="KBS12345678901234567",
        path=path,
    )
    attach_protective_stop_order_id(
        operation_id,
        "STOP-1",
        path=path,
    )
    pos["protective_stop_operation_id"] = operation_id
    return operation_id


@contextlib.contextmanager
def isolated_pending_store():
    with tempfile.TemporaryDirectory() as directory:
        original = pending_order_store.STORE_PATH
        pending_order_store.STORE_PATH = os.path.join(
            directory,
            "pending.json",
        )
        try:
            yield
        finally:
            pending_order_store.STORE_PATH = original


def test_cancel_intent_is_durable_before_api_call():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        operation_id = create_stop(path, pos)

        def before_cancel():
            record = get_protective_stop(operation_id, path=path)
            assert record["exit_coordination_requested"] is True
            assert record["cancel_api_attempted"] is True
            assert record["exit_coordination_action"] == "EXIT"

        kite = FakeKite(
            [active(), cancelled()],
            before_cancel=before_cancel,
        )
        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert result["safe_to_submit_exit"] is True
        assert result["remaining_quantity"] == 10
        assert result["clearance"]["quantity"] == 10
        assert len(kite.cancel_calls) == 1


def test_lost_cancel_response_reconciles_cancelled_history():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        create_stop(path, pos)
        kite = FakeKite(
            [active(), cancelled()],
            cancel_error=RuntimeError("cancel response lost"),
        )

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert result["safe_to_submit_exit"] is True
        assert len(kite.cancel_calls) == 1


def test_uncertain_cancel_never_repeats_and_never_clears_exit():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        create_stop(path, pos)
        kite = FakeKite(
            [active(), active(), active()],
            cancel_error=RuntimeError("network unknown"),
        )

        first = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )
        second = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert first["safe_to_submit_exit"] is False
        assert second["safe_to_submit_exit"] is False
        assert len(kite.cancel_calls) == 1


def test_full_stop_trigger_before_cancel_closes_without_clearance():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        create_stop(path, pos)
        kite = FakeKite([complete()])

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="stop",
            store_path=path,
        )

        assert result["position_closed_by_stop"] is True
        assert result["safe_to_submit_exit"] is False
        assert result["new_stop_filled_quantity"] == 10
        assert kite.cancel_calls == []


def test_partial_stop_trigger_clears_only_remaining_quantity():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        create_stop(path, pos)
        kite = FakeKite([cancelled(filled=4, average=99.4)])

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="stop",
            store_path=path,
        )

        assert result["new_stop_filled_quantity"] == 4
        assert result["remaining_quantity"] == 6
        assert result["clearance"]["quantity"] == 6
        assert kite.cancel_calls == []


def test_unknown_history_blocks_cancel_and_market_exit():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        create_stop(path, pos)
        kite = FakeKite([[]])

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert result["safe_to_submit_exit"] is False
        assert result["state"] == "CANCELLATION_UNKNOWN"
        assert kite.cancel_calls == []


def test_restart_after_reserved_cancel_only_verifies():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        operation_id = create_stop(path, pos)

        from protective_stop_store import (
            request_protective_stop_exit_coordination,
            reserve_protective_stop_cancel_attempt,
        )

        request_protective_stop_exit_coordination(
            operation_id,
            exit_action="EXIT",
            exit_reason="fixed_target",
            position_quantity=10,
            path=path,
        )
        reserve_protective_stop_cancel_attempt(
            operation_id,
            path=path,
        )
        kite = FakeKite([cancelled()])

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert result["safe_to_submit_exit"] is True
        assert kite.cancel_calls == []


def test_quantity_mismatch_fails_closed():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position(quantity=11)
        create_stop(path, pos, quantity=10)
        kite = FakeKite([active()])

        result = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="fixed_target",
            store_path=path,
        )

        assert result["safe_to_submit_exit"] is False
        assert result["state"] == "STOP_IDENTITY_UNRESOLVED"
        assert kite.cancel_calls == []


def test_live_exit_executor_rejects_missing_clearance():
    with isolated_pending_store():
        kite = FakeKite([exit_complete()])
        result = place_exit_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg(),
        )

        assert result["success"] is False
        assert "PROTECTIVE_STOP_NOT_CLEARED" in result["reason"]
        assert kite.place_calls == []


def test_live_force_exit_executor_rejects_missing_clearance():
    with isolated_pending_store():
        kite = FakeKite([exit_complete()])
        result = place_force_exit_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg(),
        )

        assert result["success"] is False
        assert "PROTECTIVE_STOP_NOT_CLEARED" in result["reason"]
        assert kite.place_calls == []


def test_exact_live_clearance_allows_one_exit_submission():
    with isolated_pending_store():
        kite = FakeKite([exit_complete()])
        clearance = {
            "safe_to_submit_exit": True,
            "paper": False,
            "symbol": "INFY",
            "exchange": "NSE",
            "quantity": 10,
            "exit_action": "EXIT",
            "protective_stop_state": "CANCELLED",
            "protective_stop_operation_id": "STOP-OP",
            "protective_stop_order_id": "STOP-1",
        }
        result = place_exit_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg(),
            protection_clearance=clearance,
        )

        assert result["success"] is True
        assert result["filled_quantity"] == 10
        assert len(kite.place_calls) == 1


def test_force_exit_cancels_stop_then_submits_one_exact_market_order():
    with tempfile.TemporaryDirectory() as directory, isolated_pending_store():
        stop_path = Path(directory) / "stops.json"
        pos = position()
        create_stop(stop_path, pos)
        kite = FakeKite(
            [active(), cancelled(), exit_complete()]
        )

        coordination = coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="FORCE_EXIT",
            exit_reason="square_off",
            store_path=stop_path,
        )
        result = place_force_exit_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg(),
            protection_clearance=coordination["clearance"],
        )

        assert coordination["safe_to_submit_exit"] is True
        assert coordination["clearance"]["exit_action"] == "FORCE_EXIT"
        assert result["success"] is True
        assert result["filled_quantity"] == 10
        assert len(kite.cancel_calls) == 1
        assert len(kite.place_calls) == 1
        assert kite.place_calls[0]["quantity"] == 10
        assert kite.place_calls[0]["transaction_type"] == "SELL"


def test_clearance_is_bound_to_exact_quantity_and_action():
    with isolated_pending_store():
        kite = FakeKite([exit_complete(quantity=9)])
        clearance = {
            "safe_to_submit_exit": True,
            "paper": False,
            "symbol": "INFY",
            "exchange": "NSE",
            "quantity": 10,
            "exit_action": "EXIT",
            "protective_stop_state": "CANCELLED",
        }

        wrong_quantity = place_exit_order(
            kite,
            "INFY",
            "BUY",
            9,
            "NSE",
            cfg(),
            protection_clearance=clearance,
        )
        wrong_action = place_force_exit_order(
            kite,
            "INFY",
            "BUY",
            10,
            "NSE",
            cfg(),
            protection_clearance=clearance,
        )

        assert wrong_quantity["success"] is False
        assert wrong_action["success"] is False
        assert kite.place_calls == []


def test_paper_exit_requires_no_broker_stop_or_clearance():
    kite = FakeKite([exit_complete()])
    result = place_exit_order(
        kite,
        "INFY",
        "BUY",
        10,
        "NSE",
        cfg(paper=True),
    )

    assert result["success"] is True
    assert result["status"] == "PAPER_FILLED"
    assert kite.place_calls == []
    assert kite.cancel_calls == []


def test_applied_stop_fill_is_monotonic():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "stops.json"
        pos = position()
        operation_id = create_stop(path, pos)
        kite = FakeKite([cancelled(filled=4, average=99.4)])

        coordinate_protective_stop_for_exit(
            kite,
            symbol="INFY",
            position=pos,
            cfg=cfg(),
            exit_action="EXIT",
            exit_reason="stop",
            store_path=path,
        )
        mark_protective_stop_fill_applied(
            operation_id,
            4,
            applied_average_price=99.4,
            path=path,
        )

        record = get_protective_stop(operation_id, path=path)
        assert record["applied_filled_quantity"] == 4
        assert record["applied_average_price"] == 99.4


@contextlib.contextmanager
def live_main_config():
    names = [
        "PAPER_TRADING",
        "VARIETY",
        "PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS",
        "PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS",
    ]
    missing = object()
    original = {
        name: getattr(main_module.cfg, name, missing)
        for name in names
    }

    main_module.cfg.PAPER_TRADING = False
    main_module.cfg.VARIETY = "regular"
    main_module.cfg.PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS = 0
    main_module.cfg.PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS = 0

    try:
        yield
    finally:
        for name, value in original.items():
            if value is missing:
                delattr(main_module.cfg, name)
            else:
                setattr(main_module.cfg, name, value)


@contextlib.contextmanager
def isolated_main_side_effects():
    original_record = main_module.record_trade
    original_save = main_module.save_positions
    trades = []
    saves = []

    main_module.record_trade = lambda *args, **kwargs: trades.append(
        {"args": args, "kwargs": kwargs}
    )
    main_module.save_positions = (
        lambda positions, *args, **kwargs: saves.append(
            {key: dict(value) for key, value in positions.items()}
        )
    )

    try:
        yield trades, saves
    finally:
        main_module.record_trade = original_record
        main_module.save_positions = original_save


def full_position(quantity=10):
    return {
        "direction": "BUY",
        "qty": quantity,
        "filled_quantity": quantity,
        "entry": 100.0,
        "entry_average_price": 100.0,
        "stop": 99.55,
        "target": 101.5,
        "exchange": "NSE",
        "protective_stop_operation_id": None,
        "protective_stop_active": True,
        "protective_stop_state": "ACTIVE",
        "protective_stop_quantity": quantity,
        "entry_protected": True,
        "automated_exit_blocked": False,
        "manual_reconciliation_required": False,
    }


def test_main_partial_stop_fill_exits_only_remaining_quantity():
    with tempfile.TemporaryDirectory() as directory:
        stop_path = Path(directory) / "stops.json"
        pos = full_position()
        operation_id = create_stop(stop_path, pos)
        kite = FakeKite([cancelled(filled=4, average=99.4)])
        open_positions = {"INFY": pos}
        risk = FakeRisk()

        with live_main_config(), isolated_main_side_effects() as (trades, _):
            preparation = main_module._prepare_protective_stop_for_exit(
                kite,
                "INFY",
                pos,
                open_positions,
                risk,
                "NSE",
                "EXIT",
                "stop",
                store_path=stop_path,
            )

        assert preparation["proceed"] is True
        assert preparation["clearance"]["quantity"] == 6
        assert open_positions["INFY"]["qty"] == 6
        assert trades[0]["args"][2] == 4
        assert len(risk.results) == 1

        record = get_protective_stop(operation_id, path=stop_path)
        assert record["applied_filled_quantity"] == 4
        assert record["resolved"] is True


def test_main_full_broker_stop_fill_removes_position_without_exit():
    with tempfile.TemporaryDirectory() as directory:
        stop_path = Path(directory) / "stops.json"
        pos = full_position()
        create_stop(stop_path, pos)
        kite = FakeKite([complete()])
        open_positions = {"INFY": pos}
        risk = FakeRisk()

        with live_main_config(), isolated_main_side_effects() as (trades, _):
            preparation = main_module._prepare_protective_stop_for_exit(
                kite,
                "INFY",
                pos,
                open_positions,
                risk,
                "NSE",
                "EXIT",
                "stop",
                store_path=stop_path,
            )

        assert preparation["position_closed"] is True
        assert preparation["proceed"] is False
        assert "INFY" not in open_positions
        assert trades[0]["args"][2] == 10
        assert kite.cancel_calls == []
        assert kite.place_calls == []


def test_main_ambiguous_cancellation_never_reaches_market_exit():
    with tempfile.TemporaryDirectory() as directory:
        stop_path = Path(directory) / "stops.json"
        pos = full_position()
        create_stop(stop_path, pos)
        kite = FakeKite(
            [active(), active()],
            cancel_error=RuntimeError("cancel response unknown"),
        )
        open_positions = {"INFY": pos}

        with live_main_config(), isolated_main_side_effects():
            preparation = main_module._prepare_protective_stop_for_exit(
                kite,
                "INFY",
                pos,
                open_positions,
                FakeRisk(),
                "NSE",
                "EXIT",
                "fixed_target",
                store_path=stop_path,
            )

        assert preparation["proceed"] is False
        assert open_positions["INFY"]["qty"] == 10
        assert open_positions["INFY"]["automated_exit_blocked"] is True
        assert kite.place_calls == []
        assert len(kite.cancel_calls) == 1


def test_crash_after_stop_fill_reservation_cannot_apply_fill_twice():
    with tempfile.TemporaryDirectory() as directory:
        stop_path = Path(directory) / "stops.json"
        first_position = full_position()
        operation_id = create_stop(stop_path, first_position)
        first_positions = {"INFY": first_position}
        kite = FakeKite([cancelled(filled=4, average=99.4)])
        original_record = main_module.record_trade
        original_save = main_module.save_positions

        main_module.record_trade = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                RuntimeError("simulated crash before position save")
            )
        )
        main_module.save_positions = lambda *args, **kwargs: None

        try:
            with live_main_config():
                try:
                    main_module._prepare_protective_stop_for_exit(
                        kite,
                        "INFY",
                        first_position,
                        first_positions,
                        FakeRisk(),
                        "NSE",
                        "EXIT",
                        "stop",
                        store_path=stop_path,
                    )
                except RuntimeError as exc:
                    assert "simulated crash" in str(exc)
                else:
                    raise AssertionError("simulated crash did not occur")
        finally:
            main_module.record_trade = original_record
            main_module.save_positions = original_save

        record = get_protective_stop(operation_id, path=stop_path)
        assert record["applied_filled_quantity"] == 4

        # Simulate restart from the last durable position snapshot: the
        # local quantity is conservatively still 10, while the stop store
        # proves that four broker fills were already reserved locally.
        restarted_position = full_position()
        restarted_position["protective_stop_operation_id"] = operation_id
        restarted_positions = {"INFY": restarted_position}
        restarted_kite = FakeKite(
            [cancelled(filled=4, average=99.4)]
        )

        with live_main_config(), isolated_main_side_effects() as (trades, _):
            retry = main_module._prepare_protective_stop_for_exit(
                restarted_kite,
                "INFY",
                restarted_position,
                restarted_positions,
                FakeRisk(),
                "NSE",
                "EXIT",
                "stop",
                store_path=stop_path,
            )

        assert retry["proceed"] is False
        assert retry["status"] == "STOP_IDENTITY_UNRESOLVED"
        assert restarted_positions["INFY"]["qty"] == 10
        assert trades == []
        assert restarted_kite.cancel_calls == []
        assert restarted_kite.place_calls == []


def test_position_monitor_applies_triggered_broker_stop_before_price_fetch():
    with tempfile.TemporaryDirectory() as directory:
        stop_path = Path(directory) / "stops.json"
        original_store = protective_stop_store.STORE_PATH
        original_fetch = main_module.fetch_candles
        pos = full_position()
        create_stop(stop_path, pos)
        kite = FakeKite([complete()])
        open_positions = {"INFY": pos}
        risk = FakeRisk()

        protective_stop_store.STORE_PATH = stop_path
        main_module.fetch_candles = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                AssertionError("price fetch must not run after full stop fill")
            )
        )

        try:
            with live_main_config(), isolated_main_side_effects() as (trades, _):
                status = main_module.check_position_exit(
                    kite,
                    "INFY",
                    {"INFY": 123},
                    {"INFY": "NSE"},
                    open_positions,
                    risk,
                )
        finally:
            protective_stop_store.STORE_PATH = original_store
            main_module.fetch_candles = original_fetch

        assert "INFY" not in open_positions
        assert "CLOSED (protective_stop)" in status
        assert trades[0]["args"][2] == 10
        assert kite.place_calls == []


def test_restart_resumes_cancelled_stop_exit_without_market_data():
    from protective_stop_store import (
        request_protective_stop_exit_coordination,
        reserve_protective_stop_cancel_attempt,
    )

    with (
        tempfile.TemporaryDirectory() as directory,
        isolated_pending_store(),
    ):
        stop_path = Path(directory) / "stops.json"
        original_store = protective_stop_store.STORE_PATH
        original_fetch = main_module.fetch_candles
        pos = full_position()
        operation_id = create_stop(stop_path, pos)
        open_positions = {"INFY": pos}
        kite = FakeKite(
            [cancelled(), cancelled(), exit_complete()]
        )

        request_protective_stop_exit_coordination(
            operation_id,
            exit_action="EXIT",
            exit_reason="fixed_target",
            position_quantity=10,
            path=stop_path,
        )
        reserve_protective_stop_cancel_attempt(
            operation_id,
            path=stop_path,
        )

        protective_stop_store.STORE_PATH = stop_path
        main_module.fetch_candles = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                AssertionError(
                    "durable exit resumption must not require a quote"
                )
            )
        )

        try:
            with live_main_config(), isolated_main_side_effects():
                status = main_module.check_position_exit(
                    kite,
                    "INFY",
                    {"INFY": 123},
                    {"INFY": "NSE"},
                    open_positions,
                    FakeRisk(),
                )
        finally:
            protective_stop_store.STORE_PATH = original_store
            main_module.fetch_candles = original_fetch

        assert "CLOSED (fixed_target)" in status
        assert "INFY" not in open_positions
        assert kite.cancel_calls == []
        assert len(kite.place_calls) == 1


def test_main_orders_coordination_before_all_market_exit_calls():
    source = Path("main.py").read_text(encoding="utf-8")
    normal_prepare = source.index(
        "preparation = _prepare_protective_stop_for_exit(",
        source.index("def check_position_exit("),
    )
    normal_exit = source.index(
        "exit_result = place_exit_order(",
        normal_prepare,
    )
    force_prepare = source.index(
        "preparation = _prepare_protective_stop_for_exit(",
        source.index("if past_square_off():"),
    )
    force_exit = source.index(
        "place_force_exit_order(",
        force_prepare,
    )

    assert normal_prepare < normal_exit
    assert force_prepare < force_exit


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    for test in tests:
        test()
        print(f"PASS: {test.__name__}")

    print(
        "PROTECTIVE-STOP EXIT COORDINATION TESTS PASSED "
        f"({len(tests)})"
    )
