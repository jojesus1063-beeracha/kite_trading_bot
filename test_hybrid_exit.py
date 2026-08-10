from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

import main as main_module
from entry_protection import build_confirmed_position, protect_confirmed_position
from hybrid_exit import RUNNER_PENDING, SCALP_PENDING
from strategy import Signal


class FakeRisk:
    def __init__(self):
        self.results = []

    def record_trade_result(self, pnl):
        self.results.append(pnl)


def cfg(**changes):
    values = {
        "PAPER_TRADING": True,
        "ENABLE_FIXED_TARGET": True,
        "ENABLE_HYBRID_EXIT": True,
        "HYBRID_SCALP_FRACTION": 0.50,
        "HYBRID_SCALP_R": 1.0,
        "HYBRID_RUNNER_R": 2.0,
        "HYBRID_MOVE_STOP_TO_BREAKEVEN": True,
        "STOP_LOSS_PERCENT": 1.0,
        "PROFIT_TARGET_PERCENT": 2.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def signal(direction="BUY"):
    return Signal(
        symbol="TEST",
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0 if direction == "BUY" else 101.0,
        target=102.0 if direction == "BUY" else 98.0,
        timestamp=pd.Timestamp("2026-08-10 10:00"),
        reason="test",
    )


def result(quantity=10):
    return {
        "filled_quantity": quantity,
        "average_price": 100.0,
        "requested_quantity": quantity,
        "status": "PAPER_FILLED",
        "order_id": "PAPER",
        "operation_id": None,
        "client_tag": None,
    }


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


buy = build_confirmed_position(signal(), result(), "NSE", cfg())
check("paper position enables hybrid exit", buy["hybrid_exit_enabled"])
check("ten shares split five and five", buy["hybrid_scalp_quantity"] == 5 and buy["hybrid_runner_quantity"] == 5)
check("BUY scalp target is 1R", buy["hybrid_scalp_target"] == 101.0)
check("BUY runner target is 2R", buy["hybrid_runner_target"] == 102.0)
check("scalp target is active first", buy["target"] == 101.0 and buy["hybrid_exit_stage"] == SCALP_PENDING)

sell = build_confirmed_position(signal("SELL"), result(), "NSE", cfg())
check("SELL targets are below entry", sell["hybrid_scalp_target"] == 99.0 and sell["hybrid_runner_target"] == 98.0)

live = build_confirmed_position(signal(), result(), "NSE", cfg(PAPER_TRADING=False))
check("live mode receives the same hybrid split", live["hybrid_exit_enabled"])
check(
    "live hybrid starts blocked pending broker protection",
    live["automated_exit_blocked"] and live["protective_stop_state"] == "PENDING",
)

live_runner = dict(live)
live_runner.update({
    "qty": 5,
    "filled_quantity": 10,
    "hybrid_exit_stage": RUNNER_PENDING,
    "stop": 100.0,
    "protective_stop_operation_id": None,
})
stop_calls = []

def fake_stop_placer(_kite, **kwargs):
    stop_calls.append(kwargs)
    return {
        "success": True,
        "active": True,
        "triggered": False,
        "confirmation_pending": False,
        "state": "ACTIVE",
        "operation_id": "RUNNER-STOP-OP",
        "order_id": "RUNNER-STOP",
        "client_tag": "KBSRUNNER",
        "trigger_price": 100.0,
        "requested_quantity": kwargs["quantity"],
    }

protect_confirmed_position(
    MagicMock(),
    "TEST",
    live_runner,
    cfg(PAPER_TRADING=False),
    stop_placer=fake_stop_placer,
)
check("live runner reprotection covers remaining half", stop_calls[0]["quantity"] == 5)
check("live runner requests exact break-even trigger", stop_calls[0]["trigger_price_override"] == 100.0)
check("verified runner stop unblocks live automation", live_runner["entry_protected"] and not live_runner["automated_exit_blocked"])

disabled = build_confirmed_position(signal(), result(), "NSE", cfg(ENABLE_HYBRID_EXIT=False))
check("feature toggle preserves existing fixed target", not disabled["hybrid_exit_enabled"] and disabled["target"] == 102.0)

non_fixed = build_confirmed_position(signal(), result(), "NSE", cfg(ENABLE_FIXED_TARGET=False))
check("hybrid exit cannot attach outside fixed-target mode", not non_fixed["hybrid_exit_enabled"])

single = build_confirmed_position(signal(), result(1), "NSE", cfg())
check("single-share position falls back to fixed target", not single["hybrid_exit_enabled"] and single["target"] == 102.0)

originals = {
    "paper": main_module.cfg.PAPER_TRADING,
    "fixed": main_module.cfg.ENABLE_FIXED_TARGET,
    "fetch": main_module.fetch_candles,
    "sleep": main_module.time.sleep,
    "prepare": main_module._prepare_protective_stop_for_exit,
    "exit": main_module.place_exit_order,
    "save": main_module.save_positions,
    "record": main_module.record_trade,
    "resolve_stop": main_module.mark_protective_stop_resolved,
    "protect": main_module.protect_confirmed_position,
}

try:
    main_module.cfg.PAPER_TRADING = True
    main_module.cfg.ENABLE_FIXED_TARGET = True
    main_module.time.sleep = lambda *_: None
    main_module.save_positions = lambda *_args, **_kwargs: None
    main_module.record_trade = lambda *_args, **_kwargs: None
    main_module._prepare_protective_stop_for_exit = lambda *_args, **_kwargs: {
        "proceed": True,
        "position_closed": False,
        "clearance": None,
        "status": "PAPER",
    }

    prices = [101.0]
    main_module.fetch_candles = lambda *_args, **_kwargs: pd.DataFrame([
        {"date": pd.Timestamp("2026-08-10 10:05"), "open": prices[0], "high": prices[0], "low": prices[0], "close": prices[0], "volume": 1}
    ])

    exit_quantities = []
    def paper_exit(_kite, _symbol, _direction, quantity, _exchange, _cfg, **_kwargs):
        exit_quantities.append(quantity)
        return {
            "success": True,
            "order_id": "PAPER",
            "operation_id": None,
            "status": "PAPER_FILLED",
            "reason": None,
            "requested_quantity": quantity,
            "filled_quantity": quantity,
            "average_price": None,
            "exit_confirmation_pending": False,
            "resolved": True,
        }
    main_module.place_exit_order = paper_exit

    positions = {"TEST": buy}
    risk = FakeRisk()
    first = main_module.check_position_exit(MagicMock(), "TEST", {"TEST": 1}, {"TEST": "NSE"}, positions, risk)
    check("first target exits only scalp half", exit_quantities == [5] and positions["TEST"]["qty"] == 5)
    check("first target advances durable runner state", positions["TEST"]["hybrid_exit_stage"] == RUNNER_PENDING and positions["TEST"]["target"] == 102.0)
    check("runner stop moves to break-even", positions["TEST"]["stop"] == 100.0)
    check("first result is a partial hybrid scalp exit", "PARTIAL EXIT (hybrid_scalp_1r)" in first)

    prices[0] = 102.0
    second = main_module.check_position_exit(MagicMock(), "TEST", {"TEST": 1}, {"TEST": "NSE"}, positions, risk)
    check("runner target exits remaining half", exit_quantities == [5, 5] and "TEST" not in positions)
    check("runner result is identified", "CLOSED (hybrid_runner_2r)" in second)
finally:
    main_module.cfg.PAPER_TRADING = originals["paper"]
    main_module.cfg.ENABLE_FIXED_TARGET = originals["fixed"]
    main_module.fetch_candles = originals["fetch"]
    main_module.time.sleep = originals["sleep"]
    main_module._prepare_protective_stop_for_exit = originals["prepare"]
    main_module.place_exit_order = originals["exit"]
    main_module.save_positions = originals["save"]
    main_module.record_trade = originals["record"]
    main_module.mark_protective_stop_resolved = originals["resolve_stop"]
    main_module.protect_confirmed_position = originals["protect"]

resolved_stop_ids = []
reprotection_calls = []
try:
    main_module.cfg.PAPER_TRADING = False
    main_module.save_positions = lambda *_args, **_kwargs: None
    main_module.mark_protective_stop_resolved = (
        lambda operation_id, **_kwargs: resolved_stop_ids.append(operation_id)
    )

    def fake_reprotect(_kite, _symbol, position, _cfg, **_kwargs):
        reprotection_calls.append(position.get("protective_stop_operation_id"))
        position["protective_stop_state"] = "ACTIVE"
        position["protective_stop_active"] = True
        position["entry_protected"] = True
        position["automated_exit_blocked"] = False
        return {"active": True}

    main_module.protect_confirmed_position = fake_reprotect
    runner = dict(live_runner)
    runner["protective_stop_operation_id"] = "OLD-FULL-SIZE-STOP"
    runner["protective_stop_exit_state"] = "CLEARED_FOR_EXIT"
    runner_positions = {"TEST": runner}

    reprotected = main_module._reprotect_remaining_live_position(
        MagicMock(),
        "TEST",
        runner,
        runner_positions,
    )
    check(
        "old full-size stop is resolved before runner stop intent",
        resolved_stop_ids == ["OLD-FULL-SIZE-STOP"]
        and reprotection_calls == [None],
    )
    check("runner reprotection completes", reprotected)
finally:
    main_module.cfg.PAPER_TRADING = originals["paper"]
    main_module.save_positions = originals["save"]
    main_module.mark_protective_stop_resolved = originals["resolve_stop"]
    main_module.protect_confirmed_position = originals["protect"]

print("Hybrid exit tests passed.")
