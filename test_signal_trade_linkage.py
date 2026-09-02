import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import daily_report
from entry_protection import (
    build_confirmed_position,
    build_entry_plan,
    build_recovered_position,
)
from trade_log import record_trade


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


operation_id = "entry-operation-123"
signal_id = "signal-456"
analytics = {
    "signal_id": signal_id,
    "entry_operation_id": operation_id,
    "candidate_rank": 2,
    "candidate_count": 7,
    "ranking_score": 181.5,
    "entry_quality_score": 82.0,
    "entry_context_score": 8.0,
    "confirmation_count": 1,
    "adx_state": "RISING",
    "adx_delta": 1.25,
    "relative_strength_score": 4.0,
    "market_trend_reason": "OK",
    "sector_trend": "Bearish",
    "sector_trend_reason": "OK",
    "mfe_pct": 0.31,
    "mae_pct": -0.18,
}

with TemporaryDirectory() as directory:
    log_path = Path(directory) / "trades.jsonl"
    record_trade(
        "JAINREC",
        "SELL",
        12,
        324.35,
        323.85,
        1.88,
        "square_off",
        analytics=analytics,
        log_path=log_path,
    )
    stored = json.loads(log_path.read_text().strip())

check(
    "Closed trade persists the unique signal ID",
    stored["signal_id"] == signal_id,
)
check(
    "Closed trade persists the exact entry operation ID",
    stored["entry_operation_id"] == operation_id,
)
check(
    "Closed trade persists ranking and confirmation analytics",
    stored["ranking_score"] == 181.5
    and stored["confirmation_count"] == 1
    and stored["adx_state"] == "RISING",
)
check(
    "Closed trade persists MFE and MAE",
    stored["mfe_pct"] == 0.31
    and stored["mae_pct"] == -0.18,
)
check(
    "Closed trade persists market and sector diagnostics",
    stored["market_trend_reason"] == "OK"
    and stored["sector_trend"] == "Bearish"
    and stored["sector_trend_reason"] == "OK",
)

signals = [
    {
        "symbol": "JAINREC",
        "direction": "SELL",
        "entry_price": 400.0,
        "entry_operation_id": operation_id,
        "signal_id": signal_id,
        "executed": True,
        "technical_confidence": "HIGH",
    },
    {
        "symbol": "JAINREC",
        "direction": "SELL",
        "entry_price": 324.36,
        "entry_operation_id": "different-operation",
        "executed": True,
    },
]

match = daily_report.match_trade_to_signal(stored, signals)
check(
    "Exact signal ID wins even when signal and fill prices differ",
    match is signals[0],
)

operation_only_trade = dict(stored)
operation_only_trade.pop("signal_id")
check(
    "Exact entry operation ID remains a backward-compatible join key",
    daily_report.match_trade_to_signal(
        operation_only_trade,
        signals,
    ) is signals[0],
)

wrong_id_trade = dict(stored)
wrong_id_trade.pop("signal_id")
wrong_id_trade["entry_operation_id"] = "missing-operation"
check(
    "A present operation ID never falls back to a nearby price guess",
    daily_report.match_trade_to_signal(
        wrong_id_trade,
        signals,
    ) is None,
)

legacy_trade = {
    "symbol": "JAINREC",
    "direction": "SELL",
    "entry": 324.35,
}
ambiguous_legacy_signals = [
    {
        "symbol": "JAINREC",
        "direction": "SELL",
        "entry_price": 324.20,
    },
    {
        "symbol": "JAINREC",
        "direction": "SELL",
        "entry_price": 324.60,
    },
]
check(
    "Two nearby legacy signals are reported as ambiguous, not reused",
    daily_report.match_trade_to_signal(
        legacy_trade,
        ambiguous_legacy_signals,
    ) is None,
)

signal = SimpleNamespace(
    entry_price=100.0,
    stop_loss=99.55,
    target=100.70,
    timestamp="2026-08-05 10:00:00+05:30",
    direction="BUY",
)
cfg = SimpleNamespace(
    PAPER_TRADING=False,
    ENABLE_FIXED_TARGET=True,
    STOP_LOSS_PERCENT=0.45,
    PROFIT_TARGET_PERCENT=0.70,
)
entry_result = {
    "filled_quantity": 5,
    "average_price": 100.1,
    "order_id": "ORDER-1",
    "operation_id": operation_id,
    "client_tag": "TAG-1",
    "requested_quantity": 5,
    "status": "COMPLETE",
}

plan = build_entry_plan(
    signal,
    cfg,
    signal_analytics=analytics,
)
position = build_confirmed_position(
    signal,
    entry_result,
    "NSE",
    cfg,
    signal_analytics=analytics,
)

check(
    "Durable entry plan stores signal analytics for restart recovery",
    plan["signal_analytics"]["ranking_score"] == 181.5
    and plan["signal_analytics"]["sector_trend"] == "Bearish",
)
check(
    "Confirmed position carries exact signal analytics",
    position["entry_operation_id"] == operation_id
    and position["signal_id"] == signal_id
    and position["ranking_score"] == 181.5
    and position["market_trend_reason"] == "OK"
    and position["sector_trend_reason"] == "OK",
)

order_record = {
    "side": "BUY",
    "exchange": "NSE",
    "operation_id": operation_id,
    "order_id": "ORDER-1",
    "client_tag": "TAG-1",
    "requested_quantity": 5,
    "created_at": "2026-08-05T10:00:00",
    "metadata": plan,
}
execution_result = SimpleNamespace(
    filled_quantity=5,
    average_price=100.1,
    status="COMPLETE",
    terminal=True,
)
recovered = build_recovered_position(
    order_record,
    execution_result,
    cfg,
)
check(
    "Restart-recovered position preserves the same signal linkage",
    recovered["entry_operation_id"] == operation_id
    and recovered["signal_id"] == signal_id
    and recovered["ranking_score"] == 181.5,
)

original_load_trades = daily_report.load_trades
original_load_signals = daily_report.load_signals

try:
    daily_report.load_trades = lambda _date: [stored]
    daily_report.load_signals = lambda _date: signals
    row = daily_report.build_trade_reasons("2026-08-05")[0]
finally:
    daily_report.load_trades = original_load_trades
    daily_report.load_signals = original_load_signals

check(
    "Daily report identifies the exact join and exposes tuning metrics",
    row["signal_match"] == "EXACT_SIGNAL_ID"
    and row["ranking_score"] == 181.5
    and row["entry_quality_score"] == 82.0
    and row["confirmation_count"] == 1
    and row["adx_state"] == "RISING"
    and row["mfe_pct"] == 0.31
    and row["mae_pct"] == -0.18,
)

print("SIGNAL-TO-TRADE LINKAGE TESTS PASSED")
