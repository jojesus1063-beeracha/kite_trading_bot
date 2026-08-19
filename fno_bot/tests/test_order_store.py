import os
import tempfile

import pytest

from fno_bot.execution import order_store as os_


@pytest.fixture
def tmp_store_path():
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "fno_pending_orders_test.json")


def test_path_distinct_from_equity_store():
    """
    Guards the exact failure mode flagged in the architecture review:
    if this module's STORE_PATH ever collided with the equity repo's
    pending_order_store.STORE_PATH (e.g. a copy-paste that forgot to
    change the path), duplicate-order protection would silently break
    across both bots.
    """
    import pending_order_store as equity_store  # equity repo's module, same checkout
    assert os_.STORE_PATH != equity_store.STORE_PATH


def test_create_and_resolve_lifecycle(tmp_store_path):
    op_id = os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=tmp_store_path)
    assert op_id is not None
    os_.attach_broker_order_id(op_id, "BROKER123", path=tmp_store_path)
    record = os_.get_order(op_id, path=tmp_store_path)
    assert record["order_id"] == "BROKER123"
    os_.mark_order_resolved(op_id, "COMPLETE", path=tmp_store_path)
    assert os_.get_order(op_id, path=tmp_store_path)["resolved"] is True


def test_unresolved_entry_blocks_duplicate_entry(tmp_store_path):
    os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=tmp_store_path)
    with pytest.raises(os_.UnresolvedOrderExistsError):
        os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=tmp_store_path)


def test_exit_and_force_exit_share_one_lock_family(tmp_store_path):
    os_.create_order_intent("SENSEX2582577200PE", "BFO", "EXIT", "SELL", 10, path=tmp_store_path)
    with pytest.raises(os_.UnresolvedOrderExistsError):
        os_.create_order_intent("SENSEX2582577200PE", "BFO", "FORCE_EXIT", "SELL", 10, path=tmp_store_path)


def test_conflicting_broker_order_id_raises(tmp_store_path):
    op_id = os_.create_order_intent("SENSEX2582577200PE", "BFO", "ENTRY", "BUY", 10, path=tmp_store_path)
    os_.attach_broker_order_id(op_id, "BROKER123", path=tmp_store_path)
    with pytest.raises(os_.DuplicateBrokerOrderIdError):
        os_.attach_broker_order_id(op_id, "BROKER999", path=tmp_store_path)


def test_invalid_action_rejected(tmp_store_path):
    with pytest.raises(os_.InvalidOrderRecordError):
        os_.create_order_intent("SENSEX2582577200PE", "BFO", "NOT_AN_ACTION", "BUY", 10, path=tmp_store_path)
