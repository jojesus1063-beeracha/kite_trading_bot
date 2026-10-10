from types import SimpleNamespace

from matmon_final_quote_guard import evaluate_final_quote


class Buffer:
    def __init__(self, ticks):
        self.ticks = list(ticks)

    def latest(self, _symbol):
        return self.ticks[-1] if self.ticks else None


def tick(ts, bid, ask):
    return {
        "received_at": ts,
        "depth": {
            "buy": [{"price": bid, "quantity": 100}],
            "sell": [{"price": ask, "quantity": 100}],
        },
    }


def clean():
    return SimpleNamespace(
        first_bid=100.00,
        first_ask=100.10,
        last_bid=100.10,
        last_ask=100.20,
        last_received_at=10.2,
    )


def test_accepts_fresh_new_buy_quote_without_reversal():
    result = evaluate_final_quote(
        Buffer([tick(10.3, 100.10, 100.20)]), "ABC", "BUY", clean(),
        now_fn=lambda: 10.4, sleep_fn=lambda _s: None,
    )
    assert result.accepted is True
    assert result.reason == "MATMON_FINAL_QUOTE_CONFIRMED"


def test_rejects_buy_reversal_below_first_three_range():
    result = evaluate_final_quote(
        Buffer([tick(10.3, 99.95, 100.05)]), "ABC", "BUY", clean(),
        now_fn=lambda: 10.4, sleep_fn=lambda _s: None,
    )
    assert result.accepted is False
    assert result.reason == "MATMON_FINAL_BUY_REVERSED"


def test_rejects_spread_widening():
    result = evaluate_final_quote(
        Buffer([tick(10.3, 100.10, 100.50)]), "ABC", "BUY", clean(),
        now_fn=lambda: 10.4, sleep_fn=lambda _s: None,
    )
    assert result.accepted is False
    assert result.reason == "MATMON_FINAL_SPREAD_WIDENED"


def test_rejects_when_no_newer_quote_arrives():
    times = iter([10.2, 10.8])
    result = evaluate_final_quote(
        Buffer([tick(10.2, 100.10, 100.20)]), "ABC", "BUY", clean(),
        timeout_seconds=0.5,
        now_fn=lambda: next(times),
        sleep_fn=lambda _s: None,
    )
    assert result.accepted is False
    assert result.reason == "MATMON_FINAL_NEW_QUOTE_TIMEOUT"
