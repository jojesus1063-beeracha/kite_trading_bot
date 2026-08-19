import pytest

from fno_bot.execution.entry import (
    compute_entry_limit_price, slippage_pct, check_entry_preconditions, execute_entry,
)


def test_compute_entry_limit_price_matches_spec_example():
    # Spec: PE=196.80, 10% buffer -> ceiling ~216.48
    price = compute_entry_limit_price(196.80, 10.0)
    assert round(price, 2) == 216.48


def test_compute_entry_limit_price_rejects_non_positive():
    with pytest.raises(ValueError):
        compute_entry_limit_price(0.0, 10.0)


def test_slippage_pct_measures_against_original_not_previous():
    # Original reference 196.80, later candidate 250 -> ~27% slippage
    pct = slippage_pct(196.80, 250.0)
    assert round(pct, 1) == 27.0


def _preconditions(**overrides):
    base = dict(
        original_reference_price=196.80, current_best_ask=197.0, tick_age_ms=200,
        spread_pct=1.0, max_tick_age_ms=1500, max_spread_pct=3.0,
        max_entry_slippage_pct=15.0, entry_buffer_pct=10.0, signal_still_valid=True,
    )
    base.update(overrides)
    return check_entry_preconditions(**base)


def test_preconditions_ok_case():
    result = _preconditions()
    assert result.ok
    assert result.limit_price is not None


def test_preconditions_rejects_invalid_signal():
    result = _preconditions(signal_still_valid=False)
    assert not result.ok
    assert result.reason == "SIGNAL_NO_LONGER_VALID"


def test_preconditions_rejects_stale_tick():
    result = _preconditions(tick_age_ms=5000)
    assert not result.ok
    assert "STALE_TICK" in result.reason


def test_preconditions_rejects_wide_spread():
    result = _preconditions(spread_pct=10.0)
    assert not result.ok
    assert "SPREAD_TOO_WIDE" in result.reason


def test_preconditions_rejects_missing_ask():
    result = _preconditions(current_best_ask=None)
    assert not result.ok
    assert result.reason == "NO_ASK_AVAILABLE"


def test_preconditions_aborts_on_max_slippage_gap_up():
    # Premium jumped 196.80 -> 250 -- must abort, not chase (spec #7 example)
    result = _preconditions(current_best_ask=250.0)
    assert not result.ok
    assert "MAX_SLIPPAGE_EXCEEDED" in result.reason


# --- execute_entry orchestration (mocked kite) ---

class FakeKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, fill_on_attempt=1, fill_qty=None):
        self.fill_on_attempt = fill_on_attempt
        self.fill_qty = fill_qty
        self.attempt = 0
        self.orders_placed = []

    def place_order(self, **kwargs):
        self.attempt += 1
        order_id = f"ORDER{self.attempt}"
        self.orders_placed.append((order_id, kwargs))
        return order_id

    def order_history(self, order_id):
        idx = int(order_id.replace("ORDER", ""))
        if idx >= self.fill_on_attempt:
            qty = self.fill_qty if self.fill_qty is not None else self.orders_placed[idx - 1][1]["quantity"]
            return [{"status": "COMPLETE", "filled_quantity": qty, "pending_quantity": 0,
                      "cancelled_quantity": 0, "average_price": self.orders_placed[idx - 1][1]["price"]}]
        return [{"status": "OPEN", "filled_quantity": 0, "pending_quantity": self.orders_placed[idx - 1][1]["quantity"],
                  "cancelled_quantity": 0, "average_price": None}]

    def cancel_order(self, **kwargs):
        pass


class FakeCfg:
    VARIETY = "regular"
    PRODUCT = "MIS"
    ORDER_TYPE_ENTRY = "LIMIT"
    MARKET_PROTECTION = -1
    MAX_ENTRY_ATTEMPTS = 3
    ENTRY_RETRY_BACKOFF_MS = 0
    MAX_TICK_AGE_MS = 1500
    MAX_SPREAD_PCT = 3.0
    MAX_ENTRY_SLIPPAGE_PCT = 15.0
    ENTRY_BUFFER_PCT = 10.0
    ORDER_VERIFY_MAX_WAIT_SECONDS = 1
    ORDER_VERIFY_POLL_INTERVAL_SECONDS = 0.01


@pytest.fixture(autouse=True)
def isolated_order_store(tmp_path, monkeypatch):
    from fno_bot.execution import order_store as os_
    monkeypatch.setattr(os_, "STORE_PATH", str(tmp_path / "orders.json"))
    yield


def _fresh_market(**overrides):
    base = {"best_ask": 197.0, "tick_age_ms": 200, "spread_pct": 1.0}
    base.update(overrides)
    return base


def test_execute_entry_fills_on_first_attempt():
    kite = FakeKite(fill_on_attempt=1)
    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(),
        signal_still_valid_fn=lambda: True,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "FILLED"
    assert result.filled_quantity == 10
    assert result.attempts_made == 1


def test_execute_entry_retries_then_fills():
    kite = FakeKite(fill_on_attempt=2)
    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(),
        signal_still_valid_fn=lambda: True,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.attempts_made == 2


def test_execute_entry_aborts_when_signal_invalidates():
    kite = FakeKite(fill_on_attempt=99)
    calls = {"n": 0}

    def signal_valid():
        calls["n"] += 1
        return calls["n"] == 1  # valid on attempt 1, invalid after

    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(),
        signal_still_valid_fn=signal_valid,
        sleep_fn=lambda s: None,
    )
    assert not result.success
    assert result.status == "ABORTED"
    assert result.abort_reason == "SIGNAL_NO_LONGER_VALID"


def test_execute_entry_aborts_on_extreme_slippage():
    kite = FakeKite(fill_on_attempt=99)
    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(best_ask=250.0),  # spec's chase example
        signal_still_valid_fn=lambda: True,
        sleep_fn=lambda s: None,
    )
    assert not result.success
    assert result.status == "ABORTED"
    assert "MAX_SLIPPAGE_EXCEEDED" in result.abort_reason


def test_execute_entry_gives_up_after_max_attempts_no_fill():
    kite = FakeKite(fill_on_attempt=99)
    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(),
        signal_still_valid_fn=lambda: True,
        sleep_fn=lambda s: None,
    )
    assert not result.success
    assert result.status == "NO_FILL"
    assert result.attempts_made == 3


def test_execute_entry_handles_partial_fill():
    kite = FakeKite(fill_on_attempt=1, fill_qty=6)  # only 6 of 10 filled
    result = execute_entry(
        kite, symbol="SENSEX2582577200PE", exchange="BFO", quantity=10,
        original_reference_price=196.80, cfg=FakeCfg(),
        fetch_fresh_market=lambda: _fresh_market(),
        signal_still_valid_fn=lambda: True,
        sleep_fn=lambda s: None,
    )
    assert result.success
    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_quantity == 6
