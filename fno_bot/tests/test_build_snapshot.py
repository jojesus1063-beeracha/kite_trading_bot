from fno_bot.market_data.tick_store import TickStore, normalize_kite_tick
from fno_bot.strategies.opening_scalper import build_snapshot


def _tick(token, price, bid=None, ask=None):
    raw = {"instrument_token": token, "last_price": price}
    if bid is not None and ask is not None:
        raw["depth"] = {"buy": [{"price": bid, "quantity": 10}], "sell": [{"price": ask, "quantity": 10}]}
    return normalize_kite_tick(raw, clock_fn=lambda: 0.0)


def test_build_snapshot_none_when_underlying_missing():
    store = TickStore(clock_fn=lambda: 0.0)
    store.update(_tick(2, 307.30))
    store.update(_tick(3, 196.80))
    assert build_snapshot(store, underlying_token=1, ce_token=2, pe_token=3, underlying_prev_close=77000.0) is None


def test_build_snapshot_none_when_ce_or_pe_missing():
    store = TickStore(clock_fn=lambda: 0.0)
    store.update(_tick(1, 77218.05))
    store.update(_tick(2, 307.30))
    assert build_snapshot(store, underlying_token=1, ce_token=2, pe_token=3, underlying_prev_close=77000.0) is None


def test_build_snapshot_assembles_underlying_price_correctly():
    store = TickStore(clock_fn=lambda: 0.0)
    store.update(_tick(1, 77218.05))
    store.update(_tick(2, 307.30))
    store.update(_tick(3, 196.80))
    snapshot = build_snapshot(store, underlying_token=1, ce_token=2, pe_token=3, underlying_prev_close=77000.0)
    assert snapshot is not None
    assert snapshot.underlying_price == 77218.05
    assert snapshot.ce_price == 307.30
    assert snapshot.pe_price == 196.80
    assert snapshot.underlying_prev_close == 77000.0
