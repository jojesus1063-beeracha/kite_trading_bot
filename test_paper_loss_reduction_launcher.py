import json
from pathlib import Path

import pandas as pd

import config as cfg
import paper_loss_reduction_launcher as loss
import paper_contrarian_launcher as contrarian


def test_loss_reduction_constants():
    assert loss.PAPER_NO_NEW_ENTRIES_AT_OR_AFTER == "14:00"
    assert loss.PAPER_NO_ENTRY_AFTER == "13:59"
    assert loss.PAPER_CONSECUTIVE_LOSS_BLOCK == 2
    assert loss.PAPER_EARLY_FAILURE_MIN_AGE_MINUTES == 10.0
    assert loss.PAPER_EARLY_FAILURE_MAX_MFE_PCT == 0.15


def test_apply_loss_reduction_config(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    loss.apply_loss_reduction_config()
    assert cfg.NO_ENTRY_AFTER == "13:59"
    assert cfg.PAPER_CONSECUTIVE_LOSS_BLOCK == 2
    assert cfg.PAPER_EARLY_FAILURE_MAX_MFE_PCT == 0.15


def test_loss_reduction_launcher_installs_daily_risk_guard_before_main_import():
    source = (Path(__file__).resolve().parent / "paper_loss_reduction_launcher.py").read_text(
        encoding="utf-8"
    )
    install_pos = source.index("install_paper_daily_risk_guard()")
    main_import_pos = source.index("import main as trading_main")
    assert install_pos < main_import_pos


def _write_trade(path: Path, signal_id, pnl, time_text, symbol="ABC"):
    with path.open("a", encoding="utf-8") as h:
        h.write(json.dumps({
            "date": "2026-08-11",
            "time": time_text,
            "symbol": symbol,
            "signal_id": signal_id,
            "pnl": pnl,
        }) + "\n")


def test_two_consecutive_loss_guard_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    loss.apply_loss_reduction_config()
    path = tmp_path / "history.jsonl"
    _write_trade(path, "s1", -5.0, "10:00:00")
    _write_trade(path, "s2", -4.0, "11:00:00")

    # Install after the current ADX installer because the current installer
    # intentionally neutralizes legacy frequency guards.
    loss.install_consecutive_loss_guard()
    ok, detail = contrarian._paper_entry_guard(
        "ABC",
        now=pd.Timestamp("2026-08-11 12:00:00", tz="Asia/Kolkata"),
        log_path=path,
    )
    assert ok is False
    assert detail["reason"] == "CONSECUTIVE_SYMBOL_LOSSES"
    assert detail["consecutive_completed_losses"] == 2


def test_winner_resets_consecutive_loss_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "PAPER_TRADING", True, raising=False)
    path = tmp_path / "history.jsonl"
    _write_trade(path, "s1", -5.0, "10:00:00")
    _write_trade(path, "s2", -4.0, "11:00:00")
    _write_trade(path, "s3", 3.0, "12:00:00")

    loss.install_consecutive_loss_guard()
    ok, detail = contrarian._paper_entry_guard(
        "ABC",
        now=pd.Timestamp("2026-08-11 13:00:00", tz="Asia/Kolkata"),
        log_path=path,
    )
    assert ok is True
    assert detail["consecutive_completed_losses"] == 0
