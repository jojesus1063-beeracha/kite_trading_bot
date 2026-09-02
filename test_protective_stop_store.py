from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from protective_stop_store import (
    UnresolvedProtectiveStopExistsError,
    attach_protective_stop_order_id,
    create_protective_stop_intent,
    get_protective_stop,
    list_unresolved_protective_stops,
    mark_protective_stop_resolved,
    update_protective_stop_verification,
)


with TemporaryDirectory() as directory:
    path = Path(directory) / "stops.json"

    operation_id = create_protective_stop_intent(
        symbol="INFY",
        exchange="NSE",
        position_direction="BUY",
        stop_side="SELL",
        requested_quantity=4,
        trigger_price=1493.25,
        client_tag="KBS12345678901234567",
        path=path,
    )

    assert len(
        list_unresolved_protective_stops(path)
    ) == 1

    try:
        create_protective_stop_intent(
            symbol="INFY",
            exchange="NSE",
            position_direction="BUY",
            stop_side="SELL",
            requested_quantity=4,
            trigger_price=1493.25,
            client_tag="KBS12345678901234567",
            path=path,
        )
    except UnresolvedProtectiveStopExistsError:
        pass
    else:
        raise AssertionError(
            "Duplicate unresolved stop was accepted"
        )

    attach_protective_stop_order_id(
        operation_id,
        "STOP-1",
        path=path,
    )

    result = SimpleNamespace(
        filled_quantity=0,
        pending_quantity=4,
        cancelled_quantity=0,
        average_price=None,
        status="TRIGGER PENDING",
        active=True,
        terminal=False,
        status_message=None,
        exchange_order_id="EX-1",
        verified_at=SimpleNamespace(
            isoformat=lambda: "2026-08-04T00:00:00+00:00"
        ),
        history_attempts=1,
        api_error_count=0,
    )

    update_protective_stop_verification(
        operation_id,
        result,
        path=path,
    )

    record = get_protective_stop(
        operation_id,
        path=path,
    )

    assert record["order_id"] == "STOP-1"
    assert record["client_tag"] == (
        "KBS12345678901234567"
    )
    assert record["active"] is True
    assert record["last_known_status"] == (
        "TRIGGER PENDING"
    )
    assert record["requested_quantity"] == 4

    mark_protective_stop_resolved(
        operation_id,
        resolution_reason="test complete",
        path=path,
    )

    assert not list_unresolved_protective_stops(path)
    assert path.stat().st_mode & 0o777 == 0o600

print("PROTECTIVE STOP STORE TESTS PASSED")
