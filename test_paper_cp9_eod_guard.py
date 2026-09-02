from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

import config as cfg
import paper_cp9_eod_guard as cp9
import paper_cp9_eod_launcher as launcher


@pytest.fixture(autouse=True)
def paper_cp9_config(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    monkeypatch.setattr(cfg, "PAPER_CP9_EOD_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "PAPER_CP9_CHECKPOINT_MINUTES", 9.0, raising=False)
    monkeypatch.setattr(cfg, "PAPER_CP9_MAE_THRESHOLD_PCT", -0.20, raising=False)


def test_checkpoint_does_not_evaluate_before_9m():
    evaluated, trigger = cp9.cp9_checkpoint_decision(8.999, -0.50, -0.30, False)
    assert evaluated is False
    assert trigger is False


def test_checkpoint_triggers_exactly_at_9m_boundary():
    evaluated, trigger = cp9.cp9_checkpoint_decision(9.0, -0.20, -0.0001, False)
    assert evaluated is True
    assert trigger is True


def test_checkpoint_does_not_trigger_when_mae_is_not_severe_enough():
    evaluated, trigger = cp9.cp9_checkpoint_decision(9.0, -0.199999, -0.10, False)
    assert evaluated is True
    assert trigger is False


def test_checkpoint_requires_current_loss_strictly_below_zero():
    evaluated, trigger = cp9.cp9_checkpoint_decision(9.2, -0.30, 0.0, False)
    assert evaluated is True
    assert trigger is False


def test_checkpoint_never_retriggers_after_evaluated():
    evaluated, trigger = cp9.cp9_checkpoint_decision(15.0, -1.0, -1.0, True)
    assert evaluated is True
    assert trigger is False


def test_lock_persists_same_day_and_is_ignored_next_day(tmp_path, monkeypatch):
    lock_path = tmp_path / "cp9_eod_locks.json"
    audit_path = tmp_path / "cp9_eod_audit.jsonl"
    monkeypatch.setattr(cp9, "LOCK_PATH", lock_path)
    monkeypatch.setattr(cp9, "AUDIT_PATH", audit_path)

    now = pd.Timestamp("2026-08-12 10:00:00", tz="Asia/Kolkata")
    cp9._lock_symbol("ABC", {"mae_pct": -0.25}, now=now)

    locked, detail = cp9.is_symbol_locked("ABC", now=now)
    assert locked is True
    assert detail["reason"] == "CP9_MAE20_FAILED_DEVELOPMENT_EOD_LOCK"

    tomorrow = pd.Timestamp("2026-08-13 09:30:00", tz="Asia/Kolkata")
    locked_next_day, detail_next_day = cp9.is_symbol_locked("ABC", now=tomorrow)
    assert locked_next_day is False
    assert detail_next_day is None


def test_entry_guard_blocks_only_locked_symbol(tmp_path, monkeypatch):
    lock_path = tmp_path / "cp9_eod_locks.json"
    audit_path = tmp_path / "cp9_eod_audit.jsonl"
    monkeypatch.setattr(cp9, "LOCK_PATH", lock_path)
    monkeypatch.setattr(cp9, "AUDIT_PATH", audit_path)

    now = pd.Timestamp("2026-08-12 11:00:00", tz="Asia/Kolkata")
    cp9._lock_symbol("BLOCKME", now=now)

    def allow(symbol, now=None, log_path=None):
        return True, {"decision": "ALLOW", "source": "base"}

    base = SimpleNamespace(_paper_entry_guard=allow)
    cp9.install_cp9_eod_entry_guard(base)

    ok, detail = base._paper_entry_guard("BLOCKME", now=now)
    assert ok is False
    assert detail["reason"] == "CP9_POST_FAILURE_EOD_LOCK"

    ok2, detail2 = base._paper_entry_guard("OTHER", now=now)
    assert ok2 is True
    assert detail2["cp9_eod_locked"] is False


def test_corrupt_lock_state_fails_closed_for_entry(tmp_path, monkeypatch):
    lock_path = tmp_path / "cp9_eod_locks.json"
    audit_path = tmp_path / "cp9_eod_audit.jsonl"
    lock_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(cp9, "LOCK_PATH", lock_path)
    monkeypatch.setattr(cp9, "AUDIT_PATH", audit_path)

    def allow(symbol, now=None, log_path=None):
        return True, {"decision": "ALLOW"}

    base = SimpleNamespace(_paper_entry_guard=allow)
    cp9.install_cp9_eod_entry_guard(base)
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    ok, detail = base._paper_entry_guard("ABC", now=now)
    assert ok is False
    assert detail["reason"] == "CP9_EOD_LOCK_STATE_UNREADABLE"


def test_launcher_overrides_are_paper_only(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    launcher.apply_cp9_overrides()
    assert cfg.PAPER_CP9_EOD_ENABLED is True
    assert cfg.PAPER_CP9_CHECKPOINT_MINUTES == 9.0
    assert cfg.PAPER_CP9_MAE_THRESHOLD_PCT == -0.20

    monkeypatch.setattr(cfg, "PAPER_TRADING", False, raising=False)
    with pytest.raises(SystemExit):
        launcher.apply_cp9_overrides()


def test_stack_hook_order_is_cp9_before_mae():
    events = []

    class Base:
        @staticmethod
        def install_two_indicator_patch():
            events.append("two")

    def install_mae(trading_main):
        events.append("mae")

    stack = SimpleNamespace(base=Base, install_mae_adverse_exit_patch=install_mae)
    module = SimpleNamespace(
        install_cp9_eod_entry_guard=lambda base: events.append("entry_guard"),
        install_cp9_eod_exit_patch=lambda trading_main: events.append("cp9_exit"),
    )

    launcher.install_stack_hooks(stack, module)
    stack.base.install_two_indicator_patch()
    stack.install_mae_adverse_exit_patch(object())

    assert events == ["two", "entry_guard", "cp9_exit", "mae"]
