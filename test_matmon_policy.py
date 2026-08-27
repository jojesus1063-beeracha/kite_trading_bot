import time
from ws_ticker import TickBuffer
from matmon_entry_policy import evaluate
from matmon_quote_confirmation import evaluate_quote_window


def _tick(ts, bid, ask):
    return {"received_at": ts, "depth": {"buy": [{"price": bid, "quantity": 100}], "sell": [{"price": ask, "quantity": 100}]}}


def test_buy_all_pass():
    d = evaluate(ema9=101, ema21=100, plus_di=30, minus_di=15,
                 first_bid=100, first_ask=100.1, last_bid=100.1, last_ask=100.2)
    assert d.accepted and d.direction == "BUY"


def test_sell_all_pass():
    d = evaluate(ema9=99, ema21=100, plus_di=10, minus_di=25,
                 first_bid=100, first_ask=100.1, last_bid=99.9, last_ask=100.0)
    assert d.accepted and d.direction == "SELL"


def test_di_disagrees():
    d = evaluate(ema9=101, ema21=100, plus_di=10, minus_di=20,
                 first_bid=100, first_ask=100.1, last_bid=100.1, last_ask=100.2)
    assert not d.accepted and d.reason == "DI_DISAGREES"


def test_one_sided_quote_rejected():
    d = evaluate(ema9=101, ema21=100, plus_di=30, minus_di=15,
                 first_bid=100, first_ask=100.1, last_bid=100.1, last_ask=100.1)
    assert not d.accepted


def test_equal_ema_rejected():
    d = evaluate(ema9=100, ema21=100, plus_di=30, minus_di=15,
                 first_bid=100, first_ask=100.1, last_bid=100.1, last_ask=100.2)
    assert not d.accepted and d.direction is None


def test_quote_window_buy_and_stale():
    b = TickBuffer()
    now = time.time()
    b.append("ABC", _tick(now - 3.2, 100.0, 100.1))
    b.append("ABC", _tick(now - 0.1, 100.1, 100.2))
    e = evaluate_quote_window(b, "ABC", "BUY", window_seconds=3.0, max_age_seconds=2.0, now=now)
    assert e.confirmed
    stale = evaluate_quote_window(b, "ABC", "BUY", window_seconds=3.0, max_age_seconds=2.0, now=now + 10)
    assert not stale.confirmed


def test_crossed_book_rejected():
    b = TickBuffer(); now = time.time()
    b.append("ABC", _tick(now - 3.2, 101, 100))
    b.append("ABC", _tick(now - 0.1, 102, 101))
    e = evaluate_quote_window(b, "ABC", "BUY", window_seconds=3.0, max_age_seconds=2.0, now=now)
    assert not e.confirmed
