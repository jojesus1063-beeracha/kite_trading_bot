import pytest

from fno_bot.broker.paper_broker import PaperBroker, simulate_buy_fill_price, simulate_sell_fill_price


def test_simulate_buy_fill_capped_at_limit():
    # market ask worse than our limit -- broker would never fill above the limit
    price = simulate_buy_fill_price(limit_price=216.50, best_ask=250.0, slippage_pct=10.0)
    assert price == 216.50


def test_simulate_buy_fill_uses_slipped_ask_when_better_than_limit():
    price = simulate_buy_fill_price(limit_price=250.0, best_ask=200.0, slippage_pct=5.0)
    assert round(price, 2) == 210.0


def test_simulate_buy_fill_falls_back_to_limit_when_no_ask():
    assert simulate_buy_fill_price(200.0, None, 5.0) == 200.0


def test_simulate_sell_fill_capped_at_limit():
    price = simulate_sell_fill_price(limit_price=180.0, best_bid=150.0, slippage_pct=5.0)
    assert price == 180.0


def test_simulate_sell_fill_uses_slipped_bid_when_better_than_limit():
    price = simulate_sell_fill_price(limit_price=180.0, best_bid=200.0, slippage_pct=5.0)
    assert round(price, 2) == 190.0


class FakeRealKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def margins(self):
        return {"equity": {"available": {"live_balance": 100000}}}


class FakeCfg:
    PAPER_SLIPPAGE_PCT = 0.5


class FakeTickStore:
    def __init__(self, ticks):
        self._ticks = ticks

    def latest(self, token):
        return self._ticks.get(token)


class FakeTick:
    def __init__(self, best_bid, best_ask):
        self.best_bid = best_bid
        self.best_ask = best_ask


def test_paper_broker_never_calls_real_place_order():
    real_kite = FakeRealKite()
    tick_store = FakeTickStore({101: FakeTick(196.5, 197.0)})
    broker = PaperBroker(real_kite, tick_store, FakeCfg())
    broker.register_instrument_token("SENSEX2582577200PE", 101)

    order_id = broker.place_order(
        variety="regular", exchange="BFO", tradingsymbol="SENSEX2582577200PE",
        transaction_type="BUY", quantity=10, product="MIS", order_type="LIMIT", price=216.50,
    )
    assert order_id.startswith("PAPER")
    history = broker.order_history(order_id)
    assert history[0]["status"] == "COMPLETE"
    assert history[0]["filled_quantity"] == 10
    assert history[0]["average_price"] <= 216.50  # never worse than the submitted limit


def test_paper_broker_positions_always_empty():
    broker = PaperBroker(FakeRealKite(), FakeTickStore({}), FakeCfg())
    assert broker.positions() == {"net": [], "day": []}


def test_paper_broker_delegates_unknown_attrs_to_real_kite():
    broker = PaperBroker(FakeRealKite(), FakeTickStore({}), FakeCfg())
    assert broker.margins()["equity"]["available"]["live_balance"] == 100000


def test_paper_broker_cancel_marks_unfilled_order_cancelled():
    broker = PaperBroker(FakeRealKite(), FakeTickStore({}), FakeCfg())
    order_id = "PAPER_FAKE"
    broker._orders[order_id] = {"status": "OPEN", "filled_quantity": 0, "pending_quantity": 10,
                                  "cancelled_quantity": 0, "average_price": None,
                                  "status_message": None, "exchange_order_id": order_id}
    broker.cancel_order(variety="regular", order_id=order_id)
    assert broker._orders[order_id]["status"] == "CANCELLED"
