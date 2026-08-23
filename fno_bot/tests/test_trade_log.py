import os
import tempfile

import pytest

from fno_bot.reporting import trade_log


@pytest.fixture
def tmp_paths():
    with tempfile.TemporaryDirectory() as d:
        yield {"log": os.path.join(d, "trades.jsonl"), "status": os.path.join(d, "status.json")}


def test_record_trade_computes_net_pnl(tmp_paths):
    ok = trade_log.record_trade(
        underlying="SENSEX", strike=77200, option_type="PE", direction="BUY", quantity=100,
        entry_price=205.86, exit_price=226.65, mode="PAPER", exit_reason="PROFIT_TARGET",
        mfe_pct=12.0, mae_pct=-1.0, log_path=tmp_paths["log"],
    )
    assert ok
    history = trade_log.get_trade_history(log_path=tmp_paths["log"])
    assert len(history) == 1
    trade = history[0]
    assert trade["result"] == "WIN"
    assert trade["gross_pnl"] > 0
    assert trade["net_pnl"] < trade["gross_pnl"]  # costs deducted
    assert trade["mode"] == "PAPER"


def test_record_trade_marks_loss_correctly(tmp_paths):
    trade_log.record_trade(
        underlying="SENSEX", strike=77200, option_type="PE", direction="BUY", quantity=100,
        entry_price=205.86, exit_price=195.0, mode="PAPER", exit_reason="HARD_STOP_LOSS",
        log_path=tmp_paths["log"],
    )
    history = trade_log.get_trade_history(log_path=tmp_paths["log"])
    assert history[0]["result"] == "LOSS"


def test_get_today_summary_aggregates(tmp_paths):
    trade_log.record_trade(underlying="SENSEX", strike=77200, option_type="PE", direction="BUY",
                            quantity=100, entry_price=200.0, exit_price=220.0, mode="PAPER",
                            exit_reason="PROFIT_TARGET", log_path=tmp_paths["log"])
    trade_log.record_trade(underlying="SENSEX", strike=77200, option_type="CE", direction="BUY",
                            quantity=100, entry_price=300.0, exit_price=285.0, mode="PAPER",
                            exit_reason="HARD_STOP_LOSS", log_path=tmp_paths["log"])
    summary = trade_log.get_today_summary(log_path=tmp_paths["log"])
    assert summary["count"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1


def test_save_and_load_bot_status_roundtrip(tmp_paths):
    ok = trade_log.save_bot_status(state="MONITOR", mode="PAPER", underlying="SENSEX",
                                     session_summary={"foo": "bar"}, status_path=tmp_paths["status"])
    assert ok
    loaded = trade_log.load_bot_status(status_path=tmp_paths["status"])
    assert loaded["state"] == "MONITOR"
    assert loaded["session_summary"] == {"foo": "bar"}


def test_load_bot_status_missing_file_returns_none(tmp_paths):
    assert trade_log.load_bot_status(status_path=tmp_paths["status"]) is None
