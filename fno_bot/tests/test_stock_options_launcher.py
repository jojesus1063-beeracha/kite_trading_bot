from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.stock_option_universe import StockOptionPair, StockOptionUnderlying
from fno_bot.instruments.strike_selector import StrikeSelection
from fno_bot.market_data.tick_store import NormalizedTick, TickStore
from fno_bot.stock_options_launcher import rank_candidates


def _tick(token, price, bid, ask, bid_qty=100, ask_qty=100):
    return NormalizedTick(token, price, bid, ask, bid_qty, ask_qty, 1000, None, 0.0)


def _pair(symbol, base):
    expiry = date(2026, 8, 27)
    ce = ContractRecord(f"{symbol}CE", "NFO", base + 1, symbol, expiry, 100.0, "CE", 100, 0.05, "NFO-OPT")
    pe = ContractRecord(f"{symbol}PE", "NFO", base + 2, symbol, expiry, 100.0, "PE", 100, 0.05, "NFO-OPT")
    underlying = StockOptionUnderlying(symbol, base, (ce, pe), expiry)
    selection = StrikeSelection(100.0, 5, 100.0, expiry, ce, pe)
    return StockOptionPair(underlying, selection)


def test_rank_candidates_prefers_higher_authorized_confidence(monkeypatch):
    from fno_bot import stock_options_launcher as module
    monkeypatch.setattr(module.cfg, "AUTHORIZED_SIGNAL", "premium_imbalance")
    monkeypatch.setattr(module.cfg, "MAX_TICK_AGE_MS", 1500)
    monkeypatch.setattr(module.cfg, "MAX_SPREAD_PCT", 3.0)

    store = TickStore(clock_fn=lambda: 0.0)
    low = _pair("LOW", 100)
    high = _pair("HIGH", 200)
    for pair in (low, high):
        base = pair.underlying.instrument_token
        store.update(_tick(base, 100.0, None, None))
    store.update(_tick(101, 110.0, 109.5, 110.5))
    store.update(_tick(102, 100.0, 99.5, 100.5))
    store.update(_tick(201, 160.0, 159.5, 160.5))
    store.update(_tick(202, 100.0, 99.5, 100.5))

    ranked = rank_candidates(store, [low, high], {"LOW": 99.0, "HIGH": 99.0})
    assert [item.pair.underlying.symbol for item in ranked] == ["HIGH", "LOW"]


def test_rank_candidates_rejects_wide_spreads(monkeypatch):
    from fno_bot import stock_options_launcher as module
    monkeypatch.setattr(module.cfg, "AUTHORIZED_SIGNAL", "premium_imbalance")
    monkeypatch.setattr(module.cfg, "MAX_TICK_AGE_MS", 1500)
    monkeypatch.setattr(module.cfg, "MAX_SPREAD_PCT", 3.0)
    pair = _pair("WIDE", 300)
    store = TickStore(clock_fn=lambda: 0.0)
    store.update(_tick(300, 100.0, None, None))
    store.update(_tick(301, 120.0, 100.0, 140.0))
    store.update(_tick(302, 100.0, 99.5, 100.5))
    assert rank_candidates(store, [pair], {"WIDE": 99.0}) == []
