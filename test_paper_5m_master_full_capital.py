from types import SimpleNamespace

import pandas as pd
import pytest

import config as cfg
import paper_5m_master_full_capital as mod
import paper_5m_master_full_capital_launcher as launcher
from candlestick_engine import EntryGateResult, GateState, Pattern, Side, TradePlan, Trigger
from strategy import Signal


def _three_minute_df():
    start = pd.Timestamp("2026-08-12 09:15", tz="Asia/Kolkata")
    rows = []
    for i in range(40):
        o = 100.0 + i * 0.02
        c = o + (0.05 if i % 2 == 0 else -0.03)
        rows.append({
            "date": start + pd.Timedelta(minutes=3 * i),
            "open": o,
            "high": max(o, c) + 0.10,
            "low": min(o, c) - 0.10,
            "close": c,
            "volume": 1000 + i * 10,
        })
    return pd.DataFrame(rows)


def _signal(direction="BUY"):
    return Signal(
        symbol="TEST",
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0 if direction == "BUY" else 101.0,
        target=102.0 if direction == "BUY" else 98.0,
        timestamp=pd.Timestamp("2026-08-12 10:00", tz="Asia/Kolkata"),
        reason="upstream",
        confidence=None,
    )


def _plan(direction="BUY", trigger=Trigger.BREAKOUT):
    side = Side(direction)
    entry = 100.0
    stop = 99.0 if side == Side.BUY else 101.0
    target = 102.0 if side == Side.BUY else 98.0
    return TradePlan(
        pattern=Pattern.HAMMER if side == Side.BUY else Pattern.SHOOTING_STAR,
        side=side,
        trigger=trigger,
        setup_index=10,
        entry_index=11,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=10,
        risk_per_share=1.0,
        planned_risk=10.0,
        rr=2.0,
        reason="test plan",
    )


def test_full_capital_quantity_uses_up_to_100_percent_cash():
    assert mod.full_capital_quantity(5000, 893.75) == 5
    assert 5 * 893.75 <= 5000
    assert 6 * 893.75 > 5000


def test_full_capital_quantity_never_exceeds_cash():
    for price in (1.0, 37.25, 100.0, 893.75, 4999.0, 6000.0):
        qty = mod.full_capital_quantity(5000, price)
        assert qty * price <= 5000 + 1e-9
        if price <= 5000:
            assert (qty + 1) * price > 5000 - 1e-9


def test_resample_3m_to_5m_uses_first_max_min_last_sum():
    src = pd.DataFrame([
        {"date": pd.Timestamp("2026-08-12 09:15", tz="Asia/Kolkata"), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 100},
        {"date": pd.Timestamp("2026-08-12 09:18", tz="Asia/Kolkata"), "open": 101, "high": 103, "low": 100, "close": 102, "volume": 200},
    ])
    out = mod.resample_completed_3m_to_5m(src)
    first = out.iloc[0]
    assert first.open == pytest.approx(100)
    assert first.high == pytest.approx(103)
    assert first.low == pytest.approx(99)
    assert first.close == pytest.approx(102)
    assert first.volume == pytest.approx(300)


def test_apply_plan_preserves_direction_and_sets_geometric_2r_levels():
    s = _signal("BUY")
    p = _plan("BUY")
    updated = mod._apply_plan_to_signal(s, p)
    assert updated.direction == "BUY"
    assert updated.entry_price == pytest.approx(100.0)
    assert updated.stop_loss == pytest.approx(99.0)
    assert updated.target == pytest.approx(102.0)
    assert (updated.target - updated.entry_price) == pytest.approx(
        2 * (updated.entry_price - updated.stop_loss)
    )


def test_launcher_overrides_are_paper_only_and_force_single_full_capital_position(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "MAX_OPEN_POSITIONS", 999)
    monkeypatch.setattr(cfg, "MAX_POSITION_SIZE_PCT", 20.0)
    monkeypatch.setattr(cfg, "ENABLE_FIXED_TARGET", True)
    launcher.apply_tested_paper_overrides()
    assert cfg.PAPER_MASTER_CANDLESTICK_GATE is True
    assert cfg.PAPER_MASTER_CANDLESTICK_TIMEFRAME == "5minute"
    assert cfg.PAPER_MASTER_CANDLESTICK_SOURCE == "RESAMPLED_FROM_COMPLETED_3MINUTE"
    assert cfg.PAPER_FULL_CAPITAL_PER_TRADE is True
    assert cfg.PAPER_CAPITAL_FRACTION_PER_TRADE == pytest.approx(1.0)
    assert cfg.MAX_POSITION_SIZE_PCT == pytest.approx(100.0)
    assert cfg.MAX_OPEN_POSITIONS == 1
    assert cfg.ENABLE_FIXED_TARGET is False
    assert cfg.ENABLE_PRICE_ACTION is True
    assert cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True


def test_launcher_refuses_live_mode(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    with pytest.raises(SystemExit):
        launcher.apply_tested_paper_overrides()


def test_install_full_capital_sizing_changes_paper_but_not_live(monkeypatch):
    class FakeRisk:
        def __init__(self, cfg_obj):
            self.cfg = cfg_obj
        def position_size(self, entry, stop):
            return 7

    upstream = lambda *args, **kwargs: None
    fake_main = SimpleNamespace(evaluate=upstream, RiskManager=FakeRisk)

    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "PAPER_FULL_CAPITAL_PER_TRADE", True, raising=False)
    monkeypatch.setattr(cfg, "CAPITAL", 5000.0)
    mod.reset_runtime_state()
    mod.install_on_trading_main(fake_main)

    r = FakeRisk(cfg)
    assert r.position_size(893.75, 901.25) == 5

    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    assert r.position_size(893.75, 901.25) == 7


def test_confirmed_gate_returns_upstream_direction_with_master_levels(monkeypatch):
    upstream_signal = _signal("SELL")

    class FakeRisk:
        def __init__(self, cfg_obj): self.cfg = cfg_obj
        def position_size(self, entry, stop): return 1

    def upstream(*args, **kwargs):
        return upstream_signal

    fake_main = SimpleNamespace(evaluate=upstream, RiskManager=FakeRisk)
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "PAPER_FULL_CAPITAL_PER_TRADE", True, raising=False)
    monkeypatch.setattr(cfg, "CAPITAL", 5000.0)

    result = EntryGateResult(
        state=GateState.CONFIRMED,
        intended_direction="SELL",
        reason="CONFIRMED_PATTERN",
        pattern=Pattern.SHOOTING_STAR,
        setup=None,
        plan=_plan("SELL"),
    )
    monkeypatch.setattr(mod, "_evaluate_gate", lambda *args, **kwargs: result)
    mod.reset_runtime_state()
    mod.install_on_trading_main(fake_main)

    out = fake_main.evaluate("TEST", pd.DataFrame(), _three_minute_df(), pd.DataFrame(), cfg)
    assert out is not None
    assert out.direction == "SELL"
    assert out.stop_loss == pytest.approx(101.0)
    assert out.target == pytest.approx(98.0)


def test_no_pattern_blocks_order_signal(monkeypatch):
    class FakeRisk:
        def __init__(self, cfg_obj): self.cfg = cfg_obj
        def position_size(self, entry, stop): return 1

    fake_main = SimpleNamespace(evaluate=lambda *a, **k: _signal("BUY"), RiskManager=FakeRisk)
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "PAPER_FULL_CAPITAL_PER_TRADE", True, raising=False)
    result = EntryGateResult(GateState.NO_PATTERN, "BUY", "NO_DIRECTION_MATCHING_PATTERN")
    monkeypatch.setattr(mod, "_evaluate_gate", lambda *a, **k: result)
    mod.reset_runtime_state()
    mod.install_on_trading_main(fake_main)
    assert fake_main.evaluate("TEST", pd.DataFrame(), _three_minute_df(), pd.DataFrame(), cfg) is None


def test_waiting_sends_no_order_and_freezes_upstream_signal(monkeypatch):
    class FakeRisk:
        def __init__(self, cfg_obj): self.cfg = cfg_obj
        def position_size(self, entry, stop): return 1

    original = _signal("BUY")
    fake_main = SimpleNamespace(evaluate=lambda *a, **k: original, RiskManager=FakeRisk)
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "PAPER_FULL_CAPITAL_PER_TRADE", True, raising=False)
    result = EntryGateResult(GateState.WAITING, "BUY", "PATTERN_FOUND_WAITING_CONFIRMATION", Pattern.HAMMER)
    monkeypatch.setattr(mod, "_evaluate_gate", lambda *a, **k: result)
    mod.reset_runtime_state()
    mod.install_on_trading_main(fake_main)
    assert fake_main.evaluate("TEST", pd.DataFrame(), _three_minute_df(), pd.DataFrame(), cfg) is None
    assert mod._PENDING_UPSTREAM_SIGNALS["TEST"].direction == "BUY"


def test_next_open_confirmation_fails_closed(monkeypatch):
    class FakeRisk:
        def __init__(self, cfg_obj): self.cfg = cfg_obj
        def position_size(self, entry, stop): return 1

    fake_main = SimpleNamespace(evaluate=lambda *a, **k: _signal("BUY"), RiskManager=FakeRisk)
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "PAPER_FULL_CAPITAL_PER_TRADE", True, raising=False)
    result = EntryGateResult(
        GateState.CONFIRMED,
        "BUY",
        "CONFIRMED_PATTERN",
        Pattern.BULLISH_ENGULFING,
        None,
        _plan("BUY", trigger=Trigger.NEXT_OPEN),
    )
    monkeypatch.setattr(mod, "_evaluate_gate", lambda *a, **k: result)
    mod.reset_runtime_state()
    mod.install_on_trading_main(fake_main)
    assert fake_main.evaluate("TEST", pd.DataFrame(), _three_minute_df(), pd.DataFrame(), cfg) is None
