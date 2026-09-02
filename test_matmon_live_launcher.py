from types import SimpleNamespace

import pytest

import matmon_live_launcher as live


def _cfg(**overrides):
    values = dict(
        PAPER_TRADING=False,
        PRODUCT="MIS",
        CAPITAL=5000.0,
        MARKET_PROTECTION=-1,
        ENABLE_WS_CANDLES=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_blocks_when_paper_trading_true(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg(PAPER_TRADING=True))
    monkeypatch.delenv(live.LIVE_ACK_ENV, raising=False)
    with pytest.raises(SystemExit, match="paper_trading=false"):
        live.enforce_live_limits()


def test_blocks_without_ack_env(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg())
    monkeypatch.delenv(live.LIVE_ACK_ENV, raising=False)
    with pytest.raises(SystemExit, match="acknowledge real orders"):
        live.enforce_live_limits()


def test_blocks_wrong_product(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg(PRODUCT="CNC"))
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    with pytest.raises(SystemExit, match="PRODUCT=MIS"):
        live.enforce_live_limits()


def test_blocks_zero_capital(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg(CAPITAL=0.0))
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    with pytest.raises(SystemExit, match="TRADING_CAPITAL must be positive"):
        live.enforce_live_limits()


def test_blocks_missing_market_protection(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg(MARKET_PROTECTION=None))
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    with pytest.raises(SystemExit, match="MARKET_PROTECTION must be configured"):
        live.enforce_live_limits()


def test_blocks_without_ws_candles(monkeypatch):
    monkeypatch.setattr(live, "cfg", _cfg(ENABLE_WS_CANDLES=False))
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    with pytest.raises(SystemExit, match="ENABLE_WS_CANDLES=True"):
        live.enforce_live_limits()


def test_passes_and_sets_live_caps_when_fully_armed(monkeypatch):
    cfg_obj = _cfg()
    monkeypatch.setattr(live, "cfg", cfg_obj)
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    limits = live.enforce_live_limits()

    assert limits["strategy"] == "MATMON_HAELOHIM"
    assert cfg_obj.RISK_PER_TRADE_PCT == live.LIVE_RISK_PER_TRADE_PCT
    assert cfg_obj.MAX_OPEN_POSITIONS == live.LIVE_MAX_OPEN_POSITIONS
    assert cfg_obj.MAX_TRADES_PER_DAY == live.LIVE_MAX_TRADES_PER_DAY
    assert cfg_obj.MAX_DAILY_LOSS_PCT == live.LIVE_MAX_DAILY_LOSS_PCT
    # Unlike the existing combined-strategy launcher, the daily-loss kill
    # switch must actually be enabled here -- this is new code, not a
    # legacy quirk to preserve.
    assert cfg_obj.DAILY_LOSS_KILL_SWITCH_ENABLED is True
    assert cfg_obj.MATMON_EMA_FAST == 3
    assert cfg_obj.MATMON_EMA_SLOW == 15
    assert cfg_obj.MATMON_DI_PERIOD == 14
