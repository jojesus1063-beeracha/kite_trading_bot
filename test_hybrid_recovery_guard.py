"""Regression tests for unresolved restart recovery and hybrid planning."""

from types import SimpleNamespace

import pytest

from entry_protection import build_recovered_position
from hybrid_exit import SCALP_PENDING, configure_hybrid_exit


def cfg(**changes):
    values = {
        "PAPER_TRADING": False,
        "ENABLE_FIXED_TARGET": True,
        "ENABLE_HYBRID_EXIT": True,
        "HYBRID_SCALP_FRACTION": 0.50,
        "HYBRID_SCALP_R": 1.0,
        "HYBRID_RUNNER_R": 2.0,
        "HYBRID_MOVE_STOP_TO_BREAKEVEN": True,
        "STOP_LOSS_PERCENT": 0.45,
        "PROFIT_TARGET_PERCENT": 1.5,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def position(**changes):
    values = {
        "direction": "BUY",
        "qty": 4,
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
    }
    values.update(changes)
    return values


def recovered_order(*, quantity=4):
    return {
        "side": "BUY",
        "exchange": "NSE",
        "created_at": "2026-08-12T10:00:00+05:30",
        "operation_id": "ENTRY-OP-RECOVERY",
        "order_id": "ENTRY-RECOVERY",
        "client_tag": "KBE-RECOVERY",
        "requested_quantity": quantity,
        "metadata": {
            "fixed_target_enabled": True,
            "stop_loss_percent": 0.45,
            "profit_target_percent": 1.5,
            "signal_target_price": 102.0,
            "tick_size": 0.05,
        },
    }


def execution(*, quantity=4, average=None):
    return SimpleNamespace(
        filled_quantity=quantity,
        average_price=average,
        status="COMPLETE",
        terminal=True,
    )


def test_unresolved_entry_and_stop_disable_hybrid_without_exception():
    item = position(qty=2, entry=None, stop=None)

    result = configure_hybrid_exit(item, cfg())

    assert result is item
    assert result["hybrid_exit_enabled"] is False
    assert result["entry"] is None
    assert result["stop"] is None


@pytest.mark.parametrize(
    ("entry", "stop"),
    [
        ("not-a-number", 99.0),
        (100.0, "not-a-number"),
        (float("nan"), 99.0),
        (100.0, float("inf")),
        (100.0, 0.0),
    ],
)
def test_invalid_or_nonfinite_levels_fail_closed(entry, stop):
    item = position(qty=3, entry=entry, stop=stop)

    result = configure_hybrid_exit(item, cfg())

    assert result["hybrid_exit_enabled"] is False


def test_valid_multi_share_hybrid_behavior_is_unchanged():
    item = position(qty=4, entry=100.0, stop=99.0, target=102.0)

    result = configure_hybrid_exit(item, cfg())

    assert result["hybrid_exit_enabled"] is True
    assert result["hybrid_exit_stage"] == SCALP_PENDING
    assert result["hybrid_scalp_quantity"] == 2
    assert result["hybrid_runner_quantity"] == 2
    assert result["hybrid_scalp_target"] == 101.0
    assert result["hybrid_runner_target"] == 102.0
    assert result["target"] == 101.0


def test_recovered_multi_share_fill_without_average_price_stays_manual_and_safe():
    recovered = build_recovered_position(
        recovered_order(quantity=4),
        execution(quantity=4, average=None),
        cfg(),
    )

    assert recovered["entry"] is None
    assert recovered["stop"] is None
    assert recovered["target"] is None
    assert recovered["hybrid_exit_enabled"] is False
    assert recovered["protective_stop_state"] == "ENTRY_PRICE_UNRESOLVED"
    assert recovered["manual_reconciliation_required"] is True
    assert recovered["automated_exit_blocked"] is True
    assert recovered["protective_stop_confirmation_pending"] is False


def test_recovered_single_share_unresolved_behavior_remains_safe():
    recovered = build_recovered_position(
        recovered_order(quantity=1),
        execution(quantity=1, average=None),
        cfg(),
    )

    assert recovered["qty"] == 1
    assert recovered["entry"] is None
    assert recovered["hybrid_exit_enabled"] is False
    assert recovered["manual_reconciliation_required"] is True
    assert recovered["automated_exit_blocked"] is True
