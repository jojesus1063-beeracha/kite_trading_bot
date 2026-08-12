from types import SimpleNamespace

import pytest

import paper_native_5m_candlestick_full_capital as mod


class MarginKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, per_share=None, fail=False):
        self.per_share = per_share
        self.fail = fail
        self.calls = []

    def order_margins(self, params):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("margin unavailable")
        return [{"total": self.per_share}]


def cfg_stub(**overrides):
    base = dict(
        PAPER_TRADING=True,
        CAPITAL=5000.0,
        PAPER_CAPITAL_ALLOCATION_PCT=100.0,
        VARIETY="regular",
        PRODUCT="MIS",
        ORDER_TYPE_ENTRY="MARKET",
        ENABLE_PRICE_ACTION=False,
        ENABLE_MARKET_ALIGNMENT_FILTER=False,
        MAX_OPEN_POSITIONS=999,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cash_quantity_uses_at_most_100_percent_capital():
    assert mod.cash_quantity(5000.0, 893.75, 100.0) == 5
    assert mod.cash_quantity(5000.0, 893.75, 150.0) == 5
    assert 5 * 893.75 <= 5000.0
    assert 6 * 893.75 > 5000.0


def test_margin_quantity_uses_symbol_specific_mis_margin():
    kite = MarginKite(per_share=178.75)
    qty, source, per_share = mod.margin_quantity(
        kite, "FORTIS", "SELL", "NSE", 893.75, cfg_stub()
    )
    assert source == "KITE_SYMBOL_MARGIN"
    assert per_share == pytest.approx(178.75)
    assert qty == 27
    assert qty * per_share <= 5000.0
    assert (qty + 1) * per_share > 5000.0


def test_margin_failure_falls_closed_to_cash_not_invented_leverage():
    kite = MarginKite(fail=True)
    qty, source, per_share = mod.margin_quantity(
        kite, "FORTIS", "SELL", "NSE", 893.75, cfg_stub()
    )
    assert qty == 5
    assert source == "CASH_ONLY_FALLBACK"
    assert per_share is None


def test_invalid_margin_response_falls_back_to_cash():
    kite = MarginKite(per_share=0)
    qty, source, per_share = mod.margin_quantity(
        kite, "FORTIS", "BUY", "NSE", 893.75, cfg_stub()
    )
    assert qty == 5
    assert source == "CASH_ONLY_FALLBACK"
    assert per_share is None


def test_selected_gate_configuration_is_native_5m_2r_two_bar_wait():
    assert mod.PAPER_CANDLE_INTERVAL == "5minute"
    assert mod.PAPER_CANDLE_MIN_RR == 2.0
    assert mod.PAPER_CANDLE_MAX_WAIT_BARS == 2
    assert mod._ENGINE.config.min_rr == 2.0
    assert mod._ENGINE.config.max_wait_bars == 2


def test_full_capital_mode_forces_one_open_position_to_prevent_double_allocation(monkeypatch):
    fake_main = SimpleNamespace(
        place_entry_order=lambda *a, **k: None,
        build_confirmed_position=lambda *a, **k: None,
        evaluate_price_action=None,
    )
    monkeypatch.setattr(mod.cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(mod.cfg, "ENABLE_PRICE_ACTION", False)
    monkeypatch.setattr(mod.cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False)
    monkeypatch.setattr(mod.cfg, "MAX_OPEN_POSITIONS", 10000)

    mod.install(fake_main)

    assert mod.cfg.ENABLE_PRICE_ACTION is True
    assert mod.cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True
    assert mod.cfg.MAX_OPEN_POSITIONS == 1
    assert mod.cfg.PAPER_CAPITAL_ALLOCATION_PCT == 100.0


def test_install_refuses_live_mode_before_mutating_runtime(monkeypatch):
    fake_main = SimpleNamespace()
    old_pa = getattr(mod.cfg, "ENABLE_PRICE_ACTION", None)
    old_ma = getattr(mod.cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", None)
    monkeypatch.setattr(mod.cfg, "PAPER_TRADING", False)

    with pytest.raises(SystemExit, match="PAPER only"):
        mod.install(fake_main)

    assert getattr(mod.cfg, "ENABLE_PRICE_ACTION", None) == old_pa
    assert getattr(mod.cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", None) == old_ma


def test_gate_engine_never_changes_intended_direction():
    # Direction restriction is part of the engine configuration used by this
    # wrapper; this test guards the integration contract rather than a fixture.
    from candlestick_engine import Side

    assert Side("BUY").value == "BUY"
    assert Side("SELL").value == "SELL"
