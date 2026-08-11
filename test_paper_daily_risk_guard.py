import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import config as cfg
import paper_daily_risk_guard as guard
import position_store
import risk_manager as rm


def _cfg():
    return SimpleNamespace(
        CAPITAL=5000.0,
        MAX_DAILY_LOSS_PCT=5.0,
        RISK_PER_TRADE_PCT=0.20,
        MAX_TRADES_PER_DAY=10_000,
        MAX_OPEN_POSITIONS=10_000,
    )


def _today_state(*, pnl=0.0, halted=False, reason=""):
    return {
        "date": date.today().isoformat(),
        "trades_taken": 0,
        "realized_pnl": pnl,
        "halted": halted,
        "halt_reason": reason,
    }


def _plan(entry=100.0, stop=99.55):
    return {
        "signal_entry_price": entry,
        "signal_stop_price": stop,
        "signal_target_price": 101.0,
        "fixed_target_enabled": False,
        "stop_loss_percent": 0.45,
        "profit_target_percent": 1.5,
    }


def _install(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    monkeypatch.setattr(guard, "AUDIT_PATH", tmp_path / "daily_risk_audit.jsonl")
    guard.install_paper_daily_risk_guard()


def test_pnl_above_threshold_allows_trading(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.record_trade_result(-249.99)
    assert risk.day.halted is False
    assert risk.can_take_new_trade() is True


def test_pnl_exactly_at_threshold_halts(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.record_trade_result(-250.0)
    assert risk.day.halted is True
    assert guard._is_daily_loss_reason(risk.day.halt_reason)
    assert risk.can_take_new_trade() is False


def test_pnl_below_threshold_halts(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.record_trade_result(-250.01)
    assert risk.day.halted is True
    assert risk.can_take_new_trade() is False


def test_restart_with_daily_halt_remains_halted(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    state_path.write_text(
        json.dumps(
            _today_state(
                pnl=-260.0,
                halted=True,
                reason="Daily loss limit (5.0% of capital) hit",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(guard.ALLOW_HALT_CLEAR_ENV, raising=False)

    result = guard.reconcile_startup_halt(state_path)
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["retained"] is True
    assert result["cleared"] is False
    assert saved["halted"] is True


def test_restart_halt_stays_sticky_even_if_pnl_improved(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    state_path.write_text(
        json.dumps(
            _today_state(
                pnl=-50.0,
                halted=True,
                reason="Daily loss limit (2.0% of capital) hit",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(guard.ALLOW_HALT_CLEAR_ENV, raising=False)

    result = guard.reconcile_startup_halt(state_path)
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["retained"] is True
    assert saved["halted"] is True
    assert "2.0%" in saved["halt_reason"]


def test_explicit_operator_override_can_clear_halt(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    state_path.write_text(
        json.dumps(
            _today_state(
                pnl=-260.0,
                halted=True,
                reason="Daily loss limit (5.0% of capital) hit",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(guard.ALLOW_HALT_CLEAR_ENV, "YES")

    result = guard.reconcile_startup_halt(state_path)
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["cleared"] is True
    assert saved["halted"] is False
    assert saved["halt_reason"] == ""
    assert saved["operator_halt_clear_at"]


def test_no_override_cannot_clear_halt(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    state_path.write_text(
        json.dumps(
            _today_state(
                pnl=-1.0,
                halted=True,
                reason="DAILY_LOSS_LIMIT",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(guard.ALLOW_HALT_CLEAR_ENV, "NO")

    guard.reconcile_startup_halt(state_path)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["halted"] is True


def test_aggregate_open_risk_below_budget_allows(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.day.realized_pnl = -200.0
    positions = {
        "ABC": {
            "direction": "BUY",
            "qty": 100,
            "entry": 100.0,
            "paper_strategy_stop": 99.60,
        }
    }
    # realized loss 200 + open risk 40 + proposed risk 9 = 249
    decision = guard.aggregate_risk_decision(
        risk,
        positions,
        "BUY",
        20,
        _plan(entry=100.0, stop=99.55),
    )
    assert round(decision["aggregate_risk_if_entered"], 8) == 249.0
    assert decision["allowed"] is True


def test_aggregate_open_risk_exact_budget_blocks(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.day.realized_pnl = -200.0
    positions = {
        "ABC": {
            "direction": "BUY",
            "qty": 100,
            "entry": 100.0,
            "paper_strategy_stop": 99.60,
        }
    }
    # realized loss 200 + open risk 40 + proposed risk 10 = 250 exactly.
    decision = guard.aggregate_risk_decision(
        risk,
        positions,
        "BUY",
        20,
        _plan(entry=100.0, stop=99.50),
    )
    assert round(decision["aggregate_risk_if_entered"], 8) == 250.0
    assert decision["allowed"] is False
    assert decision["boundary_policy"] == "BLOCK_WHEN_AGGREGATE_RISK_GTE_BUDGET"


def test_aggregate_open_risk_above_budget_blocks(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.day.realized_pnl = -200.0
    positions = {
        "ABC": {
            "direction": "BUY",
            "qty": 100,
            "entry": 100.0,
            "paper_strategy_stop": 99.60,
        }
    }
    decision = guard.aggregate_risk_decision(
        risk,
        positions,
        "BUY",
        20,
        _plan(entry=100.0, stop=99.45),
    )
    assert decision["aggregate_risk_if_entered"] > decision["daily_loss_budget"]
    assert decision["allowed"] is False


def test_once_daily_halt_triggers_later_profit_does_not_unhalt(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.record_trade_result(-260.0)
    assert risk.day.halted is True

    risk.record_trade_result(+200.0)
    assert risk.day.realized_pnl == -60.0
    assert risk.day.halted is True
    assert risk.can_take_new_trade() is False


def test_aggregate_guard_does_not_modify_or_force_close_existing_positions(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_cfg(), persist=False)
    risk.day.realized_pnl = -200.0
    positions = {
        "ABC": {
            "direction": "BUY",
            "qty": 100,
            "entry": 100.0,
            "paper_strategy_stop": 99.60,
            "stop": 99.25,
        }
    }
    before = copy.deepcopy(positions)

    decision = guard.aggregate_risk_decision(
        risk,
        positions,
        "BUY",
        20,
        _plan(entry=100.0, stop=99.50),
    )

    assert decision["allowed"] is False
    assert positions == before


def test_persistent_halt_metadata_is_saved(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    monkeypatch.setattr(rm, "DAY_STATE_PATH", str(state_path))

    risk = rm.RiskManager(_cfg(), persist=True)
    risk.record_trade_result(-250.0)

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["halted"] is True
    assert saved["halt_code"] == "DAILY_LOSS_LIMIT"
    assert saved["halted_at"]
    assert saved["halt_pnl"] == -250.0
    assert saved["halt_threshold"] == -250.0


def test_corrupt_same_day_state_fails_closed(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    state_path = tmp_path / "day_state.json"
    state_path.write_text("{broken", encoding="utf-8")

    try:
        guard.reconcile_startup_halt(state_path)
    except SystemExit as exc:
        assert "SAFETY BLOCK" in str(exc)
    else:
        raise AssertionError("corrupt risk state must fail closed")


def test_launcher_no_longer_contains_automatic_daily_halt_clear():
    source = (Path(__file__).resolve().parent / "paper_50pct_risk_launcher.py").read_text(
        encoding="utf-8"
    )
    assert "previous_daily_loss_halt_cleared" not in source
    assert 'state["halted"] = False' not in source
    assert "reconcile_startup_halt" in source
    assert "install_paper_daily_risk_guard" in source
