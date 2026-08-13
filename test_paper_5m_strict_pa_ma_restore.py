import types

import config as cfg
import paper_5m_master_full_capital_launcher as launcher


def test_pa_evaluator_restore_keeps_pa_enabled_and_ma_hard(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "ENABLE_PRICE_ACTION", False)
    monkeypatch.setattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False)

    restored = launcher.restore_pa_evaluator_and_ma_after_two_indicator_patch()

    assert callable(restored)
    assert cfg.ENABLE_PRICE_ACTION is True
    assert cfg.PAPER_PRICE_ACTION_OBSERVATIONAL is True
    assert cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True


def test_pa_evaluator_restore_refuses_live_mode(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    try:
        launcher.restore_pa_evaluator_and_ma_after_two_indicator_patch()
    except SystemExit:
        pass
    else:
        raise AssertionError("LIVE mode must fail closed")


def test_tested_overrides_keep_full_cash_pa_adx_observational_ma_hard(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "MAX_OPEN_POSITIONS", 999)
    monkeypatch.setattr(cfg, "MAX_POSITION_SIZE_PCT", 20.0)
    monkeypatch.setattr(cfg, "ENABLE_PRICE_ACTION", False)
    monkeypatch.setattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False)

    launcher.apply_tested_paper_overrides()

    assert cfg.PAPER_FULL_CAPITAL_PER_TRADE is True
    assert cfg.PAPER_CAPITAL_FRACTION_PER_TRADE == 1.0
    assert cfg.MAX_POSITION_SIZE_PCT == 100.0
    assert cfg.MAX_OPEN_POSITIONS == 1
    assert cfg.ENABLE_PRICE_ACTION is True
    assert cfg.PAPER_PRICE_ACTION_OBSERVATIONAL is True
    assert cfg.PAPER_ADX_OBSERVATIONAL is True
    assert cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True
    assert cfg.PAPER_MASTER_CANDLESTICK_TIMEFRAME == "5minute"


def test_observational_pa_gate_never_blocks_but_keeps_ma_untouched(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)

    calls = []

    def original_blocker(score, cfg_module=cfg):
        calls.append(score)
        return float(score) <= 0.0

    fake_main = types.SimpleNamespace(price_action_blocks_entry=original_blocker)

    original = launcher.install_observational_price_action_gate(fake_main)

    assert original is original_blocker
    assert fake_main.price_action_blocks_entry(-25.0) is False
    assert fake_main.price_action_blocks_entry(0.0) is False
    assert fake_main.price_action_blocks_entry(15.0) is False
    assert calls == []
    assert cfg.PAPER_PRICE_ACTION_OBSERVATIONAL is True


def test_observational_pa_patch_refuses_live_mode(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    fake_main = types.SimpleNamespace(price_action_blocks_entry=lambda score: True)

    try:
        launcher.install_observational_price_action_gate(fake_main)
    except SystemExit:
        pass
    else:
        raise AssertionError("LIVE mode must fail closed")


def test_observational_adx_never_blocks_and_forces_normal_ema_direction(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)

    calls = []

    def original_blocker(adx):
        return 20.0 <= float(adx) < 30.0

    def original_ema_direction(df, adx=None):
        calls.append(adx)
        return ("BUY" if adx == float("inf") else "SELL", 101.0, 100.0)

    fake_contrarian = types.SimpleNamespace(
        _adx_entry_blocked=original_blocker,
        ema_direction=original_ema_direction,
    )

    originals = launcher.install_observational_adx_policy(fake_contrarian)

    assert originals == (original_blocker, original_ema_direction)
    assert fake_contrarian._adx_entry_blocked(15.0) is False
    assert fake_contrarian._adx_entry_blocked(25.0) is False
    assert fake_contrarian._adx_entry_blocked(55.0) is False
    direction, e9, e21 = fake_contrarian.ema_direction(object(), adx=5.0)
    assert direction == "BUY"
    assert (e9, e21) == (101.0, 100.0)
    assert calls == [float("inf")]
    assert cfg.PAPER_ADX_OBSERVATIONAL is True


def test_observational_adx_patch_refuses_live_mode(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    fake_contrarian = types.SimpleNamespace(
        _adx_entry_blocked=lambda adx: True,
        ema_direction=lambda df, adx=None: ("SELL", 1.0, 2.0),
    )

    try:
        launcher.install_observational_adx_policy(fake_contrarian)
    except SystemExit:
        pass
    else:
        raise AssertionError("LIVE mode must fail closed")
