import json

import config as cfg
import paper_50pct_risk_launcher as risk_launcher
import paper_entry_guard
import paper_mae_mfe_launcher as mae
import paper_mfe_time_launcher as mfe
import paper_contrarian_launcher as contrarian


def test_loss_reduction_constants():
    assert risk_launcher.PAPER_RISK_PER_TRADE_PCT == 0.50
    assert risk_launcher.PAPER_MAX_ENTRIES_PER_DAY == 30
    assert risk_launcher.PAPER_MAX_DAILY_LOSS_PCT == 5.0
    assert risk_launcher.PAPER_EMERGENCY_STOP_PCT == 0.75
    assert risk_launcher.PAPER_MAX_TRADES_PER_SYMBOL == 2
    assert risk_launcher.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES == 30.0
    assert risk_launcher.PAPER_ADX_BLOCK_LOW == 20.0
    assert risk_launcher.PAPER_ADX_BLOCK_HIGH == 30.0


def test_emergency_stop_preserves_strategy_geometry_and_breakeven():
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

    # Once the hybrid runner moves to breakeven, rerunning the wrapper must not
    # widen it back to the emergency stop.
    buy["stop"] = 100.0
    risk_launcher._paper_apply_emergency_stop(buy)
    assert buy["stop"] == 100.0

    sell = {"direction": "SELL", "entry": 100.0, "stop": 100.45}
    risk_launcher._paper_apply_emergency_stop(sell)
    assert round(sell["stop"], 8) == 100.75


def test_mae_loss_reduction_thresholds():
    assert mae.MAE_MIN_AGE_MINUTES == 10.0
    assert mae.MAE_THRESHOLD_PCT == -0.30
    assert mae.CURRENT_LOSS_THRESHOLD_PCT == -0.15
    assert mae.MAX_MFE_FOR_FAILURE_PCT == 0.30
    assert mae.ADVERSE_CANDLES_REQUIRED == 3


def test_selected_mfe_and_dead_trade_rules():
    assert mfe._mfe_time_reason(15, 0.50, 0.10, 80) is None
    assert (
        mfe._mfe_time_reason(25, 0.45, 0.20, 55)
        == "mfe_time_giveback_20_40"
    )
    assert (
        mfe._mfe_time_reason(25, 0.55, 0.25, 45)
        == "mfe_time_lock_20_40"
    )
    assert (
        mfe._mfe_time_reason(40, 0.20, -0.10, 150)
        == "mfe_time_dead_loser_40m"
    )
    assert (
        mfe._mfe_time_reason(50, 0.35, 0.10, 60)
        == "mfe_time_late_giveback"
    )


def test_adx_20_30_block_boundaries():
    assert contrarian._adx_entry_blocked(None) is False
    assert contrarian._adx_entry_blocked(19.999) is False
    assert contrarian._adx_entry_blocked(20.0) is True
    assert contrarian._adx_entry_blocked(29.999) is True
    assert contrarian._adx_entry_blocked(30.0) is False
    assert contrarian._adx_entry_blocked(40.0) is False


def test_durable_daily_and_symbol_entry_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    monkeypatch.setattr(cfg, "PAPER_MAX_ENTRIES_PER_DAY", 30, raising=False)
    monkeypatch.setattr(cfg, "PAPER_MAX_TRADES_PER_SYMBOL", 2, raising=False)
    monkeypatch.setattr(cfg, "PAPER_LOSS_REENTRY_COOLDOWN_MINUTES", 30.0, raising=False)

    state = tmp_path / "entry_state.json"
    history = tmp_path / "trade_history.jsonl"
    now = "2026-08-12 10:00:00+05:30"

    allowed, _ = paper_entry_guard.can_enter(
        "ABC", now=now, state_path=state, trade_history_path=history
    )
    assert allowed is True

    paper_entry_guard.record_successful_entry(
        "ABC", "BUY", 10, now=now, state_path=state
    )
    paper_entry_guard.record_successful_entry(
        "ABC", "SELL", 10, now="2026-08-12 11:00:00+05:30", state_path=state
    )

    allowed, detail = paper_entry_guard.can_enter(
        "ABC",
        now="2026-08-12 12:00:00+05:30",
        state_path=state,
        trade_history_path=history,
    )
    assert allowed is False
    assert detail["reason"] == "MAX_TRADES_PER_SYMBOL"

    # Fill the remaining daily slots with distinct symbols. ABC already has 2.
    for i in range(28):
        paper_entry_guard.record_successful_entry(
            f"S{i}",
            "BUY",
            1,
            now="2026-08-12 12:01:00+05:30",
            state_path=state,
        )

    allowed, detail = paper_entry_guard.can_enter(
        "NEW",
        now="2026-08-12 12:02:00+05:30",
        state_path=state,
        trade_history_path=history,
    )
    assert allowed is False
    assert detail["reason"] == "MAX_PAPER_ENTRIES_PER_DAY"


def test_loss_reentry_cooldown_groups_hybrid_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    monkeypatch.setattr(cfg, "PAPER_MAX_ENTRIES_PER_DAY", 30, raising=False)
    monkeypatch.setattr(cfg, "PAPER_MAX_TRADES_PER_SYMBOL", 2, raising=False)
    monkeypatch.setattr(cfg, "PAPER_LOSS_REENTRY_COOLDOWN_MINUTES", 30.0, raising=False)

    state = tmp_path / "entry_state.json"
    history = tmp_path / "trade_history.jsonl"

    paper_entry_guard.record_successful_entry(
        "XYZ",
        "BUY",
        10,
        now="2026-08-12 09:30:00+05:30",
        state_path=state,
    )

    rows = [
        {
            "date": "2026-08-12",
            "time": "10:00:00",
            "symbol": "XYZ",
            "direction": "BUY",
            "entry": 100.0,
            "entry_time": "2026-08-12 09:30:00+05:30",
            "signal_id": "same-signal",
            "pnl": 5.0,
            "result": "hybrid_scalp_1r",
        },
        {
            "date": "2026-08-12",
            "time": "10:05:00",
            "symbol": "XYZ",
            "direction": "BUY",
            "entry": 100.0,
            "entry_time": "2026-08-12 09:30:00+05:30",
            "signal_id": "same-signal",
            "pnl": -8.0,
            "result": "mfe_time_late_giveback",
        },
    ]
    history.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    allowed, detail = paper_entry_guard.can_enter(
        "XYZ",
        now="2026-08-12 10:20:00+05:30",
        state_path=state,
        trade_history_path=history,
    )
    assert allowed is False
    assert detail["reason"] == "LOSS_REENTRY_COOLDOWN"
    assert detail["latest_completed_trade_pnl"] == -3.0

    allowed, detail = paper_entry_guard.can_enter(
        "XYZ",
        now="2026-08-12 10:36:00+05:30",
        state_path=state,
        trade_history_path=history,
    )
    assert allowed is True
