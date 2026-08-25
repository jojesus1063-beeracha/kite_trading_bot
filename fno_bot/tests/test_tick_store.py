from fno_bot.market_data.tick_store import TickStore, normalize_kite_tick


def _raw_tick(token=101, price=196.80, bid=196.5, ask=197.0, bid_qty=50, ask_qty=40):
    return {
        "instrument_token": token,
        "last_price": price,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty}],
            "sell": [{"price": ask, "quantity": ask_qty}],
        },
        "volume_traded": 1000,
        "buy_quantity": 5000,
        "sell_quantity": 3000,
        "oi": 25000,
    }


def test_normalize_kite_tick_extracts_depth():
    tick = normalize_kite_tick(_raw_tick(), clock_fn=lambda: 100.0)
    assert tick.instrument_token == 101
    assert tick.last_price == 196.80
    assert tick.best_bid == 196.5
    assert tick.best_ask == 197.0
    assert tick.best_bid_qty == 50
    assert tick.best_ask_qty == 40
    assert tick.total_buy_qty == 5000
    assert tick.total_sell_qty == 3000
    assert tick.open_interest == 25000


def test_normalize_kite_tick_returns_none_for_malformed_tick():
    assert normalize_kite_tick({"depth": {}}) is None
    assert normalize_kite_tick({"instrument_token": "not-an-int-and-not-numeric"}) is None


def test_normalize_kite_tick_handles_missing_depth():
    tick = normalize_kite_tick({"instrument_token": 5, "last_price": 10.0})
    assert tick is not None
    assert tick.best_bid is None
    assert tick.best_ask is None


def test_tick_store_is_fresh_within_window():
    clock = {"t": 0.0}
    store = TickStore(clock_fn=lambda: clock["t"])
    tick = normalize_kite_tick(_raw_tick(), clock_fn=lambda: clock["t"])
    store.update(tick)

    clock["t"] = 0.5  # 500ms later
    assert store.is_fresh(101, max_age_ms=1500, now=clock["t"]) is True

    clock["t"] = 2.0  # 2s later
    assert store.is_fresh(101, max_age_ms=1500, now=clock["t"]) is False


def test_tick_store_is_fresh_false_when_no_tick_at_all():
    store = TickStore(clock_fn=lambda: 0.0)
    assert store.is_fresh(999, max_age_ms=1500) is False  # fails closed, never trades on absent data


def test_tick_store_spread_pct():
    store = TickStore(clock_fn=lambda: 0.0)
    tick = normalize_kite_tick(_raw_tick(bid=196.5, ask=197.0), clock_fn=lambda: 0.0)
    store.update(tick)
    spread = store.spread_pct(101)
    assert spread is not None
    assert round(spread, 3) == round((197.0 - 196.5) / ((197.0 + 196.5) / 2) * 100, 3)


def test_tick_store_spread_pct_none_without_both_sides():
    store = TickStore(clock_fn=lambda: 0.0)
    tick = normalize_kite_tick({"instrument_token": 1, "last_price": 10.0}, clock_fn=lambda: 0.0)
    store.update(tick)
    assert store.spread_pct(1) is None
