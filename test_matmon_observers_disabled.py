from types import SimpleNamespace

import matmon_live_launcher as live
import matmon_strategy_config as strategy
import ws_integration


def test_live_launcher_disables_observers_and_keeps_socket(monkeypatch):
    cfg = SimpleNamespace(
        PAPER_TRADING=False,
        PRODUCT="MIS",
        CAPITAL=5000.0,
        MARKET_PROTECTION=-1,
        ENABLE_WS_CANDLES=True,
    )
    monkeypatch.setattr(live, "cfg", cfg)
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    live.enforce_live_limits()

    assert cfg.ENABLE_WS_CANDLES is True
    assert cfg.WS_QUOTE_DEPTH_ONLY is True
    assert cfg.WS_ENABLE_CANDLE_SHADOW_COMPARISON is False
    assert cfg.WS_ENABLE_INDICATOR_SHADOW_COMPARISON is False
    assert cfg.MATMON_ENABLE_DASHBOARD_OBSERVATION is False
    assert cfg.MATMON_ENABLE_TICK_DEADLINE_SHADOW is False
    assert cfg.MATMON_ENABLE_VALUE_SATURATION_SHADOW is False
    assert cfg.MATMON_ENABLE_POST_DI_SHADOW is False
    assert cfg.ENABLE_EQUITY_SOCKET_SHADOW is False
    assert cfg.SOCKET_SHADOW_RECORD_RAW_TICKS is False
    assert cfg.ENTRY_SCAN_SHORTLIST_SIZE == strategy.MATMON_WATCHLIST_SIZE == 120


def test_quote_depth_only_tick_handler_does_no_candle_work():
    engine = object.__new__(ws_integration.WSShadowEngine)
    engine.quote_depth_only = True
    engine.candle_builders_entry = None
    assert engine.handle_tick("TEST", {"last_price": 100.0}) is None
