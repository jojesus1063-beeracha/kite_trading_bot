import pandas as pd

import config as cfg
import paper_50pct_risk_launcher as risk_launcher
import paper_mae_mfe_launcher as mae
import paper_mfe_time_launcher as mfe
import paper_contrarian_launcher as contrarian


def test_current_paper_policy_constants():
    assert risk_launcher.PAPER_RISK_PER_TRADE_PCT == 0.20
    assert risk_launcher.PAPER_MAX_ENTRIES_PER_DAY == 10_000
    assert risk_launcher.PAPER_CORE_MAX_TRADES_PER_DAY == 10_000
    assert risk_launcher.PAPER_MAX_OPEN_POSITIONS == 10_000
    assert risk_launcher.PAPER_MAX_DAILY_LOSS_PCT == 5.0
    assert risk_launcher.PAPER_EMERGENCY_STOP_PCT == 0.75
    assert risk_launcher.PAPER_MAX_TRADES_PER_SYMBOL == 0
    assert risk_launcher.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES == 0.0
    assert risk_launcher.PAPER_ADX_BLOCK_LOW == 0.0
    assert risk_launcher.PAPER_ADX_BLOCK_HIGH == 20.0
    assert risk_launcher.PAPER_ADX_REVERSE_FROM == 20.0
    assert risk_launcher.PAPER_ADX_HIGH_NORMAL_FROM == 40.0
    assert risk_launcher.PAPER_ADX_NORMAL_FROM == 40.0


def test_risk_overrides_disable_frequency_caps(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    risk_launcher.apply_paper_risk_overrides()
    assert cfg.RISK_PER_TRADE_PCT == 0.20
    assert cfg.MAX_TRADES_PER_DAY == 10_000
    assert cfg.MAX_OPEN_POSITIONS == 10_000
    assert cfg.MAX_DAILY_LOSS_PCT == 5.0
    assert cfg.PAPER_MAX_ENTRIES_PER_DAY == 10_000
    assert cfg.PAPER_MAX_TRADES_PER_SYMBOL == 0
    assert cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES == 0.0
    assert cfg.PAPER_ADX_BLOCK_LOW == 0.0
    assert cfg.PAPER_ADX_BLOCK_HIGH == 20.0
    assert cfg.PAPER_ADX_REVERSE_FROM == 20.0
    assert cfg.PAPER_ADX_HIGH_NORMAL_FROM == 40.0


def test_emergency_stop_preserves_strategy_geometry_and_breakeven(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_EMERGENCY_STOP_PCT", 0.75, raising=False)
    buy = {
        "direction": "BUY",
        "entry": 100.0,
        "stop": 99.55,
        "hybrid_original_stop": 99.55,
        "target": 100.45,
    }
    risk_launcher._paper_apply_emergency_stop(buy)
    assert buy["paper_strategy_stop"] == 99.55
    assert buy["paper_original_stop"] == 99.55
    assert round(buy["stop"], 8) == 99.25
    assert buy["paper_emergency_stop_active"] is True

    buy["stop"] = 100.0
    risk_launcher._paper_apply_emergency_stop(buy)
    assert buy["stop"] == 100.0

    sell = {"direction": "SELL", "entry": 100.0, "stop": 100.45}
    risk_launcher._paper_apply_emergency_stop(sell)
    assert round(sell["stop"], 8) == 100.75


def test_mae_thresholds():
    assert mae.MAE_MIN_AGE_MINUTES == 10.0
    assert mae.MAE_THRESHOLD_PCT == -0.30
    assert mae.CURRENT_LOSS_THRESHOLD_PCT == -0.15
    assert mae.MAX_MFE_FOR_FAILURE_PCT == 0.30
    assert mae.ADVERSE_CANDLES_REQUIRED == 3


def test_selected_mfe_and_dead_trade_rules():
    assert mfe._mfe_time_reason(15, 0.50, 0.10, 80) is None
    assert mfe._mfe_time_reason(25, 0.45, 0.20, 55) == "mfe_time_giveback_20_40"
    assert mfe._mfe_time_reason(25, 0.55, 0.25, 45) == "mfe_time_lock_20_40"
    assert mfe._mfe_time_reason(40, 0.20, -0.10, 150) == "mfe_time_dead_loser_40m"
    assert mfe._mfe_time_reason(50, 0.35, 0.10, 60) == "mfe_time_late_giveback"


def test_adx_below_20_blocks_and_frequency_guards_remain_off(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    risk_launcher.apply_paper_risk_overrides()
    risk_launcher.install_direction_only_adx_policy()

    assert contrarian._adx_entry_blocked(None) is False
    assert contrarian._adx_entry_blocked(0.0) is True
    assert contrarian._adx_entry_blocked(10.0) is True
    assert contrarian._adx_entry_blocked(19.999) is True
    assert contrarian._adx_entry_blocked(20.0) is False
    assert contrarian._adx_entry_blocked(25.0) is False
    assert contrarian._adx_entry_blocked(39.999) is False
    assert contrarian._adx_entry_blocked(40.0) is False

    allowed, detail = contrarian._paper_entry_guard("ABC")
    assert allowed is True
    assert detail["active"] is False


def test_adx_20_to_40_reverse_and_gte40_normal(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    risk_launcher.apply_paper_risk_overrides()
    risk_launcher.install_direction_only_adx_policy()

    # Rising closes => EMA9 > EMA21. Reversed => SELL, normal => BUY.
    df = pd.DataFrame({"close": [100 + i for i in range(30)]})

    direction_missing, _, _ = contrarian.ema_direction(df, adx=None)
    direction_20, _, _ = contrarian.ema_direction(df, adx=20.0)
    direction_35, _, _ = contrarian.ema_direction(df, adx=35.0)
    direction_39999, _, _ = contrarian.ema_direction(df, adx=39.999)
    direction_40, _, _ = contrarian.ema_direction(df, adx=40.0)
    direction_45, _, _ = contrarian.ema_direction(df, adx=45.0)

    assert direction_missing == "SELL"  # unchanged fail-safe when ADX unavailable
    assert direction_20 == "SELL"       # reversed interpretation
    assert direction_35 == "SELL"       # reversed interpretation
    assert direction_39999 == "SELL"    # reversed up to but excluding 40
    assert direction_40 == "BUY"        # normal from exactly 40
    assert direction_45 == "BUY"        # normal interpretation
