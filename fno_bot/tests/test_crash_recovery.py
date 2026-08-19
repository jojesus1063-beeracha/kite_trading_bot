import pytest

from fno_bot.monitoring.crash_recovery import compute_startup_recovery_plan, requires_recovery_state
from fno_bot.execution import order_store as os_


@pytest.fixture(autouse=True)
def isolated_order_store(tmp_path, monkeypatch):
    monkeypatch.setattr(os_, "STORE_PATH", str(tmp_path / "orders.json"))
    yield tmp_path / "orders.json"


def test_clean_startup_no_recovery_needed(isolated_order_store):
    plan = compute_startup_recovery_plan(
        local_position=None, broker_net_quantity=0,
        local_pending_orders_path=str(isolated_order_store),
    )
    assert not requires_recovery_state(plan)
    assert "Clean startup" in plan.action_summary


def test_unexpected_broker_position_requires_recovery(isolated_order_store):
    plan = compute_startup_recovery_plan(
        local_position=None, broker_net_quantity=10,
        local_pending_orders_path=str(isolated_order_store),
    )
    assert requires_recovery_state(plan)
    assert plan.has_unexpected_position


def test_unresolved_pending_order_requires_recovery(isolated_order_store):
    os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=str(isolated_order_store))
    plan = compute_startup_recovery_plan(
        local_position=None, broker_net_quantity=0,
        local_pending_orders_path=str(isolated_order_store),
    )
    assert requires_recovery_state(plan)
    assert plan.has_pending_orders
    assert len(plan.unresolved_orders) == 1


def test_resolved_order_does_not_trigger_recovery(isolated_order_store):
    op_id = os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=str(isolated_order_store))
    os_.mark_order_resolved(op_id, "COMPLETE", path=str(isolated_order_store))
    plan = compute_startup_recovery_plan(
        local_position=None, broker_net_quantity=0,
        local_pending_orders_path=str(isolated_order_store),
    )
    assert not requires_recovery_state(plan)
