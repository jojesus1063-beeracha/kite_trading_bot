from types import SimpleNamespace

from entry_quality import FreshPriceValidation
from matmon_pullback_wait import is_adverse_slippage_only, wait_for_pullback


def _tick(ts, bid, ask):
    return {
        "received_at": ts,
        "last_price": (bid + ask) / 2,
        "depth": {
            "buy": [{"price": bid - i * 0.01, "quantity": 100} for i in range(5)],
            "sell": [{"price": ask + i * 0.01, "quantity": 100} for i in range(5)],
        },
    }


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class TimedBuffer:
    def __init__(self, clock, rows):
        self.clock = clock
        self.rows = rows

    def latest(self, _symbol):
        visible = [row for row in self.rows if row[0] <= self.clock.now()]
        return visible[-1][1] if visible else None


def _engine(clock, rows):
    return SimpleNamespace(
        ws_ticker=SimpleNamespace(tick_buffer=TimedBuffer(clock, rows))
    )


def test_only_specific_adverse_slippage_rejection_is_eligible():
    eligible = FreshPriceValidation(False, 100, 100.2, 0.2, 0.2,
                                    "adverse entry slippage exceeds 0.15%")
    excessive = FreshPriceValidation(False, 100, 100.4, 0.4, 0.4,
                                     "live price moved more than 0.35% from the completed signal")
    assert is_adverse_slippage_only(eligible)
    assert not is_adverse_slippage_only(excessive)


def test_buy_waits_for_new_executable_ask_then_accepts_for_reconfirmation():
    clock = Clock()
    rows = [
        (1000.0, _tick(999.9, 100.15, 100.20)),  # pre-wait: cannot qualify
        (1000.2, _tick(1000.2, 100.14, 100.18)),
        (1000.5, _tick(1000.5, 99.99, 100.04)),
    ]
    decision = wait_for_pullback(
        _engine(clock, rows), "ABC", "BUY", 100.0,
        timeout_seconds=1.0, poll_seconds=0.1,
        now_fn=clock.now, sleep_fn=clock.sleep,
        trading_window_fn=lambda: True, risk_fn=lambda: True,
    )
    assert decision.accepted
    assert decision.reason == "MATMON_PULLBACK_PRICE_RECOVERED"
    assert decision.executable_price == 100.04


def test_sell_reversal_aborts_without_authorizing_entry():
    clock = Clock()
    rows = [
        (1000.1, _tick(1000.1, 100.20, 100.22)),
    ]
    decision = wait_for_pullback(
        _engine(clock, rows), "ABC", "SELL", 100.0,
        timeout_seconds=1.0, poll_seconds=0.1,
        now_fn=clock.now, sleep_fn=clock.sleep,
    )
    assert not decision.accepted
    assert decision.reason == "MATMON_PULLBACK_REVERSED"


def test_risk_change_aborts_fail_closed():
    clock = Clock()
    decision = wait_for_pullback(
        _engine(clock, []), "ABC", "BUY", 100.0,
        now_fn=clock.now, sleep_fn=clock.sleep,
        trading_window_fn=lambda: True, risk_fn=lambda: False,
    )
    assert not decision.accepted
    assert decision.reason == "MATMON_PULLBACK_RISK_BLOCKED"
