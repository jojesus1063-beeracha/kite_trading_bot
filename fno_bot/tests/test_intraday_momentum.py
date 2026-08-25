from datetime import datetime, timedelta

from fno_bot.strategies.intraday_momentum import MinuteCandle, evaluate_intraday_momentum
from fno_bot.strategies.signal_candidates import MarketSnapshot, TickPoint


def _candles(up=True, count=45):
    start = 100.0
    step = 0.015 if up else -0.015
    return [
        MinuteCandle(i, start + step * i - 0.04, start + step * i + 0.10,
                     start + step * i - 0.10, start + step * i, 1000 + i * 50)
        for i in range(count)
    ]


def _history(start, end, buy, sell):
    return tuple(
        TickPoint(start + (end - start) * i / 20, float(i), 1000 + i * 10, 5000, buy, sell)
        for i in range(21)
    )


def _snapshot(up=True):
    ce = _history(100, 102, 8000, 2000) if up else _history(100, 99, 2000, 8000)
    pe = _history(100, 99, 2000, 8000) if up else _history(100, 102, 8000, 2000)
    return MarketSnapshot(
        underlying_price=105, underlying_prev_close=100, ce_price=ce[-1].price,
        pe_price=pe[-1].price, ce_best_bid=100, ce_best_ask=101,
        pe_best_bid=99, pe_best_ask=100, ce_best_bid_qty=100, ce_best_ask_qty=50,
        pe_best_bid_qty=50, pe_best_ask_qty=100, ce_history=ce, pe_history=pe,
    )


def test_intraday_momentum_requires_completed_candle_warmup():
    result = evaluate_intraday_momentum(_candles(count=20), _snapshot())
    assert result.direction is None
    assert "at least" in result.reason


def test_intraday_momentum_confirms_bullish_ce_with_live_option_flow():
    result = evaluate_intraday_momentum(_candles(up=True), _snapshot(up=True))
    assert result.direction == "CE"
    assert result.confidence > 0


def test_intraday_momentum_confirms_bearish_pe_with_live_option_flow():
    result = evaluate_intraday_momentum(_candles(up=False), _snapshot(up=False))
    assert result.direction == "PE"


def test_intraday_momentum_rejects_option_flow_disagreement():
    result = evaluate_intraday_momentum(_candles(up=True), _snapshot(up=False))
    assert result.direction is None
    assert "option" in result.reason
