from datetime import datetime, timedelta
import pandas as pd

from matmon_entry_policy import evaluate
from matmon_quote_confirmation import collect_quote_evidence
from ws_ticker import TickBuffer


def test_buy_accept():
    r=evaluate(ema9=101,ema21=100,plus_di=30,minus_di=15,first_bid=100,first_ask=100.05,last_bid=100.1,last_ask=100.15)
    assert r.accepted and r.direction=="BUY"


def test_sell_accept():
    r=evaluate(ema9=99,ema21=100,plus_di=15,minus_di=30,first_bid=100,first_ask=100.05,last_bid=99.9,last_ask=99.95)
    assert r.accepted and r.direction=="SELL"


def test_di_disagrees():
    r=evaluate(ema9=101,ema21=100,plus_di=10,minus_di=20,first_bid=100,first_ask=100.05,last_bid=100.1,last_ask=100.15)
    assert not r.accepted and r.reason=="DI_DISAGREES"


def test_one_sided_quote_rejected():
    r=evaluate(ema9=101,ema21=100,plus_di=30,minus_di=15,first_bid=100,first_ask=100.05,last_bid=100.1,last_ask=100.05)
    assert not r.accepted


def test_stationary_huge_bid_does_not_matter():
    r=evaluate(ema9=101,ema21=100,plus_di=30,minus_di=15,first_bid=100,first_ask=100.05,last_bid=100,last_ask=100.05)
    assert not r.accepted


def test_crossed_book_rejected():
    r=evaluate(ema9=101,ema21=100,plus_di=30,minus_di=15,first_bid=100,first_ask=99,last_bid=101,last_ask=100)
    assert not r.accepted


def test_equal_emas_rejected():
    r=evaluate(ema9=100,ema21=100,plus_di=30,minus_di=15,first_bid=100,first_ask=100.05,last_bid=100.1,last_ask=100.15)
    assert not r.accepted and r.direction is None


def _tick(ts, received, bid, ask):
    return {"exchange_timestamp":ts,"received_at":received,"depth":{"buy":[{"price":bid}],"sell":[{"price":ask}]}}


def test_quote_evidence_from_existing_buffer():
    b=TickBuffer(); t=datetime(2026,8,28,9,18,0); now=1000.0
    b.append("ABC",_tick(t,998,100,100.05)); b.append("ABC",_tick(t+timedelta(seconds=3),1000,100.1,100.15))
    q=collect_quote_evidence(b,"ABC",3.0,2.0,now)
    assert q.available and q.first_bid==100 and q.last_bid==100.1


def test_stale_quote_rejected():
    b=TickBuffer(); t=datetime(2026,8,28,9,18,0); b.append("ABC",_tick(t,990,100,100.05))
    q=collect_quote_evidence(b,"ABC",3.0,2.0,1000)
    assert not q.available and q.reason=="MATMON_STALE_QUOTE"
