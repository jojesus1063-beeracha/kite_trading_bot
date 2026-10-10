import matmon_eod_squareoff as mod

# Keep unit tests side-effect free: production audit logging is tested through
# the deployment contract and exercised only by the timed service.
mod.audit = lambda *args, **kwargs: None


class FakeKite:
    def __init__(self):
        self.qty = -40
        self.placed = []
        self.cancelled = []
        self.order_rows = []

    def positions(self):
        return {"day": [{
            "exchange": "NSE", "tradingsymbol": "IOC", "product": "MIS",
            "quantity": self.qty,
        }] if self.qty else []}

    def orders(self):
        return list(self.order_rows)

    def cancel_order(self, variety, order_id):
        self.cancelled.append((variety, order_id))
        for row in self.order_rows:
            if row.get("order_id") == order_id:
                row["status"] = "CANCELLED"

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        self.qty = 0
        return "EXIT-1"


def test_tracked_exposure_excludes_untracked_manual_mis():
    kite = FakeKite()
    kite.positions = lambda: {"day": [
        {"exchange": "NSE", "tradingsymbol": "IOC", "product": "MIS", "quantity": -40},
        {"exchange": "NSE", "tradingsymbol": "SBIN", "product": "MIS", "quantity": 5},
    ]}
    result = mod.tracked_exposure(kite, {"IOC": {"exchange": "NSE"}})
    assert set(result) == {("NSE", "IOC")}


def test_exit_side_and_tag_for_short_position():
    kite = FakeKite()
    order_id = mod.place_exit(kite, "NSE", "IOC", -40)
    assert order_id == "EXIT-1"
    assert kite.placed[0]["transaction_type"] == "BUY"
    assert kite.placed[0]["quantity"] == 40
    assert kite.placed[0]["tag"] == "matmon_eod"


def test_protective_stop_cancelled_before_exit():
    kite = FakeKite()
    kite.order_rows = [{
        "order_id": "STOP-1", "status": "TRIGGER PENDING", "variety": "regular"
    }]
    mod.cancel_protective_order(
        kite, "NSE", "IOC", {"protective_stop_order_id": "STOP-1"}
    )
    assert kite.cancelled == [("regular", "STOP-1")]


def test_existing_active_exit_prevents_duplicate():
    kite = FakeKite()
    kite.order_rows = [{
        "order_id": "EXISTING", "status": "OPEN", "exchange": "NSE",
        "tradingsymbol": "IOC", "product": "MIS", "transaction_type": "BUY",
    }]
    assert mod.place_exit(kite, "NSE", "IOC", -40) == "EXISTING"
    assert kite.placed == []
