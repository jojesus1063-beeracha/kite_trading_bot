import types

import config as cfg
import paper_5m_master_full_capital_launcher as launcher


def test_strict_pa_ma_restore_reenables_both_hard_gates(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True)
    monkeypatch.setattr(cfg, "ENABLE_PRICE_ACTION", False)
    monkeypatch.setattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False)

    restored = launcher.restore_strict_pa_ma_after_two_indicator_patch()

    assert callable(restored)
    assert cfg.ENABLE_PRICE_ACTION is True
    assert cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True


def test_strict_pa_ma_restore_refuses_live_mode(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", False)
    try:
        launcher.restore_strict_pa_ma_after_two_indicator_patch()
    except SystemExit:
        pass
    else:
        raise AssertionError("LIVE mode must fail closed")


def test_tested_overrides_keep_full_cash_and_pa_ma(monkeypatch):
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
    assert cfg.ENABLE_MARKET_ALIGNMENT_FILTER is True
    assert cfg.PAPER_MASTER_CANDLESTICK_TIMEFRAME == "5minute"
