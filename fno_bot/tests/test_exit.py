import pytest

from fno_bot.execution.exit import compute_exit_limit_price, compute_emergency_exit_price, execute_exit


def test_compute_exit_limit_price_slightly_below_bid():
    price = compute_exit_limit_price(226.65, 1.0)
    assert round(price, 4) == round(226.65 * 0.99, 4)


def test_compute_exit_limit_price_rejects_non_positive():
    with pytest.raises(ValueError):
        compute_exit_limit_price(0.0, 1.0)


def test_compute_emergency_exit_price_below_bid():
    price = compute_emergency_exit_price(200.0)
    assert price == 180.0  # 10% below


def test_compute_emergency_exit_price_floored_above_zero():
    price = compute_emergency_exit_price(0.03, tick_size=0.05)
    assert price == 0.05


class FakeKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, fill_after_reprices=0, final_fill_qty=None):
        """fill_after_reprices: how many modify_order calls happen before
        the order shows COMPLETE. 0 = fills on the very first verify."""
        self.fill_after_reprices = fill_after_reprices
        self.final_fill_qty = final_fill_qty
        self.reprice_count = 0
        self.order_id = None
        self.quantity = None
        self.price = None

    def place_order(self, **kwargs):
        self.order_id = "EXIT_ORDER_1"
        self.quantity = kwargs["quantity"]
        self.price = kwargs["price"]
        return self.order_id

    def modify_order(self, **kwargs):
        self.reprice_count += 1
        self.price = kwargs["price"]

    def order_history(self, order_id):
        qty = self.final_fill_qty if self.final_fill_qty is not None else self.quantity
        if self.reprice_count >= self.fill_after_reprices:
            return [{"status": "COMPLETE", "filled_quantity": qty, "pending_quantity": self.quantity - qty,
                      "cancelled_quantity": 0, "average_price": self.price}]
        return [{"status": "OPEN", "filled_quantity": 0, "pending_quantity": self.quantity,
                  "cancelled_quantity": 0, "average_price": None}]


class FakeCfg:
    VARIETY = "regular"
    PRODUCT = "MIS"
    MARKET_PROTECTION = -1
    EXIT_ORDER_BUFFER_PCT = 1.0
    EXIT_REPRICE_WAIT_MS = 0
    MAX_EXIT_REPRICE_ATTEMPTS = 3
    ORDER_VERIFY_MAX_WAIT_SECONDS = 1
    ORDER_VERIFY_POLL_INTERVAL_SECONDS = 0.01


@pytest.fixture(autouse=True)
def isolated_order_store(tmp_path, monkeypatch):
    from fno_bot.execution import order_store as os_
    monkeypatch.setattr(os_, "STORE_PATH", str(tmp_path / "orders.json"))
    yield


def test_execute_exit_fills_immediately_no_escalation_needed():
    kite = FakeKite(fill_after_reprices=0)
    result = execute_exit(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10, direction="BUY",
        action="EXIT", cfg=FakeCfg(), fetch_fresh_best_bid=lambda: 226.65,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "FILLED"
    assert result.filled_quantity == 10
    assert result.escalation_steps == 1


def test_execute_exit_escalates_through_reprices_then_fills():
    kite = FakeKite(fill_after_reprices=2)
    result = execute_exit(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10, direction="BUY",
        action="EXIT", cfg=FakeCfg(), fetch_fresh_best_bid=lambda: 226.65,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "FILLED"
    assert kite.reprice_count >= 2


def test_execute_exit_falls_through_to_emergency_step():
    # Never fills through the normal ladder (fill_after_reprices way beyond max) --
    # must reach the emergency marketable-price step and fill there.
    kite = FakeKite(fill_after_reprices=999)
    # Make the emergency step itself succeed by having order_history return
    # COMPLETE once reprice_count exceeds MAX_EXIT_REPRICE_ATTEMPTS (i.e. after
    # the emergency reprice, which is one more modify_order call).
    kite.fill_after_reprices = FakeCfg.MAX_EXIT_REPRICE_ATTEMPTS + 1
    result = execute_exit(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10, direction="BUY",
        action="EXIT", cfg=FakeCfg(), fetch_fresh_best_bid=lambda: 226.65,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "FILLED"


def test_execute_exit_handles_partial_fill_at_emergency_step():
    kite = FakeKite(fill_after_reprices=FakeCfg.MAX_EXIT_REPRICE_ATTEMPTS + 1, final_fill_qty=7)
    result = execute_exit(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10, direction="BUY",
        action="EXIT", cfg=FakeCfg(), fetch_fresh_best_bid=lambda: 226.65,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_quantity == 7
    assert result.remaining_quantity == 3


def test_execute_exit_never_requests_more_than_open_quantity():
    kite = FakeKite(fill_after_reprices=0)
    execute_exit(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10, direction="BUY",
        action="EXIT", cfg=FakeCfg(), fetch_fresh_best_bid=lambda: 226.65,
        sleep_fn=lambda s: None,
    )
    assert kite.quantity == 10  # never inflated beyond the requested open quantity
