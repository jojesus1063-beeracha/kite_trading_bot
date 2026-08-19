from fno_bot.monitoring.disconnect_handler import decide_disconnect_action, DisconnectAction, reconcile_position_with_broker


def test_connected_and_flat_is_ok():
    d = decide_disconnect_action(is_connected=True, just_reconnected=False,
                                   has_open_position=False, disconnected_seconds=None,
                                   recovery_timeout_seconds=30)
    assert d.action == DisconnectAction.OK


def test_just_reconnected_always_forces_reconciliation():
    d = decide_disconnect_action(is_connected=True, just_reconnected=True,
                                   has_open_position=True, disconnected_seconds=None,
                                   recovery_timeout_seconds=30)
    assert d.action == DisconnectAction.RECONCILE_WITH_BROKER


def test_disconnected_while_flat_stops_new_entries():
    d = decide_disconnect_action(is_connected=False, just_reconnected=False,
                                   has_open_position=False, disconnected_seconds=5,
                                   recovery_timeout_seconds=30)
    assert d.action == DisconnectAction.STOP_NEW_ENTRIES


def test_disconnected_with_open_position_within_timeout_attempts_reconnect():
    d = decide_disconnect_action(is_connected=False, just_reconnected=False,
                                   has_open_position=True, disconnected_seconds=10,
                                   recovery_timeout_seconds=30)
    assert d.action == DisconnectAction.ATTEMPT_RECONNECT


def test_disconnected_with_open_position_beyond_timeout_is_emergency():
    d = decide_disconnect_action(is_connected=False, just_reconnected=False,
                                   has_open_position=True, disconnected_seconds=45,
                                   recovery_timeout_seconds=30)
    assert d.action == DisconnectAction.EMERGENCY_POSITION_HANDLING


def test_reconcile_position_consistent_when_both_zero():
    result = reconcile_position_with_broker(None, 0)
    assert result["consistent"]
    assert result["action"] == "NONE"


def test_reconcile_position_flags_unexpected_broker_position():
    result = reconcile_position_with_broker(None, 10)
    assert not result["consistent"]
    assert result["action"] == "RECOVER_UNEXPECTED_POSITION"


def test_reconcile_position_flags_stale_local_position():
    result = reconcile_position_with_broker({"quantity": 10}, 0)
    assert not result["consistent"]
    assert result["action"] == "CLEAR_STALE_LOCAL_POSITION"


def test_reconcile_position_flags_quantity_mismatch():
    result = reconcile_position_with_broker({"quantity": 10}, 6)
    assert not result["consistent"]
    assert result["action"] == "RECOVER_QUANTITY_MISMATCH"
