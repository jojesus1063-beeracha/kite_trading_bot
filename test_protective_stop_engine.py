from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from protective_stop import (
    ProtectiveStopError,
    calculate_protective_trigger,
    place_protective_stop,
    verify_protective_stop_active,
)
from protective_stop_store import (
    list_unresolved_protective_stops,
)


class FakeKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_SLM = "SL-M"
    VALIDITY_DAY = "DAY"

    def __init__(
        self,
        history,
        *,
        submission_error=None,
    ):
        self.history = history
        self.submission_error = submission_error
        self.place_calls = []

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)

        if self.submission_error is not None:
            raise self.submission_error

        return "STOP-ORDER-1"

    def order_history(self, order_id):
        return self.history


cfg = SimpleNamespace(
    PAPER_TRADING=False,
    VARIETY="regular",
    PRODUCT="MIS",
    MARKET_PROTECTION=-1,
    PROTECTIVE_STOP_VERIFY_MAX_WAIT_SECONDS=0,
    PROTECTIVE_STOP_VERIFY_POLL_INTERVAL_SECONDS=0,
)

assert calculate_protective_trigger(
    confirmed_entry_price=1000,
    position_direction="BUY",
    stop_loss_percent=0.45,
    tick_size=0.05,
) == 995.50

assert calculate_protective_trigger(
    confirmed_entry_price=1000,
    position_direction="SELL",
    stop_loss_percent=0.45,
    tick_size=0.05,
) == 1004.50

active_history = [{
    "status": "TRIGGER PENDING",
    "filled_quantity": 0,
    "pending_quantity": 4,
    "cancelled_quantity": 0,
    "average_price": 0,
    "status_message": None,
    "exchange_order_id": "EX-STOP-1",
}]

with TemporaryDirectory() as directory:
    path = Path(directory) / "stops.json"
    kite = FakeKite(active_history)

    result = place_protective_stop(
        kite,
        symbol="INFY",
        position_direction="BUY",
        quantity=4,
        exchange="NSE",
        confirmed_entry_price=1500,
        stop_loss_percent=0.45,
        tick_size=0.05,
        cfg=cfg,
        store_path=path,
    )

    assert result["success"] is True
    assert result["active"] is True
    assert result["state"] == "ACTIVE"
    assert result["trigger_price"] == 1493.25

    call = kite.place_calls[0]

    assert call["transaction_type"] == "SELL"
    assert call["order_type"] == "SL-M"
    assert call["product"] == "MIS"
    assert call["trigger_price"] == 1493.25
    assert call["market_protection"] == -1
    assert call["tag"].isalnum()
    assert len(call["tag"]) == 20
    assert result["client_tag"] == call["tag"]

    unresolved = list_unresolved_protective_stops(
        path
    )

    assert len(unresolved) == 1
    assert unresolved[0]["active"] is True
    assert unresolved[0]["order_id"] == "STOP-ORDER-1"

triggered = verify_protective_stop_active(
    FakeKite([{
        "status": "COMPLETE",
        "filled_quantity": 4,
        "pending_quantity": 0,
        "cancelled_quantity": 0,
        "average_price": 995.30,
        "status_message": None,
        "exchange_order_id": "EX-2",
    }]),
    "STOP-2",
    4,
    max_wait_seconds=0,
    poll_interval_seconds=0,
)

assert triggered.state == "TRIGGERED"
assert triggered.terminal is True
assert triggered.filled_quantity == 4
assert triggered.average_price == 995.30

rejected = verify_protective_stop_active(
    FakeKite([{
        "status": "REJECTED",
        "filled_quantity": 0,
        "pending_quantity": 0,
        "cancelled_quantity": 4,
        "average_price": 0,
        "status_message": "invalid trigger",
        "exchange_order_id": None,
    }]),
    "STOP-3",
    4,
    max_wait_seconds=0,
    poll_interval_seconds=0,
)

assert rejected.state == "REJECTED"
assert rejected.active is False
assert rejected.terminal is True

paper_cfg = SimpleNamespace(
    PAPER_TRADING=True,
    VARIETY="regular",
    PRODUCT="MIS",
    MARKET_PROTECTION=-1,
)

try:
    place_protective_stop(
        FakeKite(active_history),
        symbol="INFY",
        position_direction="BUY",
        quantity=4,
        exchange="NSE",
        confirmed_entry_price=1500,
        stop_loss_percent=0.45,
        tick_size=0.05,
        cfg=paper_cfg,
    )
except ProtectiveStopError:
    pass
else:
    raise AssertionError(
        "Live broker stop was permitted in PAPER mode"
    )

with TemporaryDirectory() as directory:
    path = Path(directory) / "stops.json"

    uncertain = place_protective_stop(
        FakeKite(
            active_history,
            submission_error=RuntimeError(
                "network result unknown"
            ),
        ),
        symbol="SBIN",
        position_direction="SELL",
        quantity=3,
        exchange="NSE",
        confirmed_entry_price=800,
        stop_loss_percent=0.45,
        tick_size=0.05,
        cfg=cfg,
        store_path=path,
    )

    assert uncertain["success"] is False
    assert uncertain["status"] == (
        "SUBMISSION_UNCERTAIN"
    )
    assert uncertain["confirmation_pending"] is True

    unresolved = list_unresolved_protective_stops(
        path
    )

    assert len(unresolved) == 1
    assert unresolved[0]["order_id"] is None
    assert unresolved[0]["resolved"] is False

print("PROTECTIVE STOP ENGINE TESTS PASSED")
