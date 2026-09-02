import json
from types import SimpleNamespace

import config as cfg
import executor
import paper_daily_risk_guard as guard
import position_store
import risk_manager as rm


def _risk_cfg():
    return SimpleNamespace(
        CAPITAL=5000.0,
        MAX_DAILY_LOSS_PCT=5.0,
        RISK_PER_TRADE_PCT=0.20,
        MAX_TRADES_PER_DAY=10_000,
        MAX_OPEN_POSITIONS=10_000,
    )


def _paper_exec_cfg():
    return SimpleNamespace(PAPER_TRADING=True)


def _plan():
    return {
        "signal_entry_price": 100.0,
        "signal_stop_price": 99.55,
        "signal_target_price": 101.0,
        "fixed_target_enabled": False,
        "stop_loss_percent": 0.45,
        "profit_target_percent": 1.5,
    }


def _install(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    monkeypatch.setattr(guard, "AUDIT_PATH", tmp_path / "daily_risk_audit.jsonl")
    monkeypatch.setattr(position_store, "POSITIONS_PATH", str(tmp_path / "open_positions.json"))
    guard.install_paper_daily_risk_guard()


def test_final_entry_gate_blocks_when_aggregate_risk_reaches_budget(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_risk_cfg(), persist=False)
    risk.day.realized_pnl = -241.0

    # Proposed sizing risk = 20 * Rs0.45 = Rs9.00.
    # Realized loss 241 + proposed risk 9 = Rs250 exactly => BLOCK.
    result = executor.place_entry_order(
        None,
        "ABC",
        "BUY",
        20,
        "NSE",
        _paper_exec_cfg(),
        entry_plan=_plan(),
    )

    assert result["success"] is False
    assert result["status"] == "REJECTED"
    assert result["reason"] == "PAPER_AGGREGATE_DAILY_RISK_BUDGET"
    assert result["filled_quantity"] == 0


def test_final_entry_gate_allows_when_aggregate_risk_stays_below_budget(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_risk_cfg(), persist=False)
    risk.day.realized_pnl = -240.0

    # 240 + 9 = 249, so the wrapper must pass through to the ordinary PAPER
    # executor, which reports a PAPER_FILLED result and never touches Kite.
    result = executor.place_entry_order(
        None,
        "ABC",
        "BUY",
        20,
        "NSE",
        _paper_exec_cfg(),
        entry_plan=_plan(),
    )

    assert result["success"] is True
    assert result["status"] == "PAPER_FILLED"
    assert result["filled_quantity"] == 20


def test_existing_open_risk_is_included_by_final_entry_gate(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    risk = rm.RiskManager(_risk_cfg(), persist=False)
    risk.day.realized_pnl = -200.0

    positions_path = tmp_path / "open_positions.json"
    positions_path.write_text(
        json.dumps(
            {
                "date": guard._today(),
                "positions": {
                    "OPEN1": {
                        "direction": "BUY",
                        "qty": 100,
                        "entry": 100.0,
                        "paper_strategy_stop": 99.60,
                        "stop": 99.25,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # 200 realized loss + 40 open sizing risk + 9 proposed = 249 => allow.
    allowed = executor.place_entry_order(
        None,
        "ABC",
        "BUY",
        20,
        "NSE",
        _paper_exec_cfg(),
        entry_plan=_plan(),
    )
    assert allowed["success"] is True

    # Increase realized loss by Rs1: total becomes exactly Rs250 => block.
    risk.day.realized_pnl = -201.0
    blocked = executor.place_entry_order(
        None,
        "XYZ",
        "BUY",
        20,
        "NSE",
        _paper_exec_cfg(),
        entry_plan=_plan(),
    )
    assert blocked["success"] is False
    assert blocked["reason"] == "PAPER_AGGREGATE_DAILY_RISK_BUDGET"
