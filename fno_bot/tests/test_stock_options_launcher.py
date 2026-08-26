from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.stock_option_universe import StockOptionPair, StockOptionUnderlying
from fno_bot.instruments.strike_selector import StrikeSelection
from fno_bot.market_data.tick_store import NormalizedTick, TickStore
from fno_bot.stock_options_launcher import RankedCandidate, rank_candidates, rank_intraday_candidates
from fno_bot.strategies.signal_candidates import TickPoint


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


def test_rank_candidates_passes_history_to_confirmed_momentum(monkeypatch):
    from fno_bot import stock_options_launcher as module
    monkeypatch.setattr(module.cfg, "AUTHORIZED_SIGNAL", "confirmed_momentum")
    monkeypatch.setattr(module.cfg, "MAX_TICK_AGE_MS", 1500)
    monkeypatch.setattr(module.cfg, "MAX_SPREAD_PCT", 3.0)
    pair = _pair("MOMO", 400)
    store = TickStore(clock_fn=lambda: 0.0)
    store.update(_tick(400, 101.0, None, None))
    store.update(_tick(401, 101.0, 100.5, 101.5, 80, 20))
    store.update(_tick(402, 99.0, 98.5, 99.5, 20, 80))
    histories = {
        401: [TickPoint(100.0, 0.0), TickPoint(100.5, 1.0), TickPoint(101.0, 2.1)],
        402: [TickPoint(100.0, 0.0), TickPoint(99.5, 1.0), TickPoint(99.0, 2.1)],
    }
    ranked = rank_candidates(store, [pair], {"MOMO": 100.0}, histories)
    assert len(ranked) == 1
    assert ranked[0].authorized_result.direction == "CE"


def test_intraday_funnel_uses_completed_candles_only_for_live_shortlist(monkeypatch):
    from types import SimpleNamespace
    from fno_bot import stock_options_launcher as module

    monkeypatch.setattr(module.cfg, "INTRADAY_HISTORICAL_SHORTLIST_SIZE", 1)
    monkeypatch.setattr(module, "evaluate_intraday_momentum", lambda candles, snapshot:
                        SimpleNamespace(direction="CE", confidence=77.0,
                                        reason="confirmed", metrics={"completed_candles": len(candles)}))
    store = TickStore(clock_fn=lambda: 0.0)
    first, second = _pair("FIRST", 500), _pair("SECOND", 600)
    for pair in (first, second):
        base = pair.underlying.instrument_token
        store.update(_tick(base, 100.0, None, None))
        store.update(_tick(base + 1, 101.0, 100.5, 101.5))
        store.update(_tick(base + 2, 99.0, 98.5, 99.5))
    histories = {
        token: [TickPoint(100.0, 0.0), TickPoint(101.0, 20.0)]
        for token in (500, 501, 502, 600, 601, 602)
    }
    live = [
        RankedCandidate(first, SimpleNamespace(direction="CE"), 90.0, 1.0, 99.0),
        RankedCandidate(second, SimpleNamespace(direction="CE"), 80.0, 1.0, 99.0),
    ]

    class Cache:
        calls = []
        def completed_minute_candles(self, token):
            self.calls.append(token)
            return [object()] * 30

    cache = Cache()
    ranked = rank_intraday_candidates(live, store, histories, cache)
    assert [item.pair.underlying.symbol for item in ranked] == ["FIRST"]
    assert cache.calls == [500]
