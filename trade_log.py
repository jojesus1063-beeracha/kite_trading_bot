"""
Simple append-only trade history log, shared between main.py (writes
each closed trade) and configure_app.py (reads it for the dashboard).

Every function accepts an optional path override (log_path/status_path)
so tests can inject a temp-directory path directly rather than
monkeypatching module globals -- production callers never pass these,
so they're completely unaffected and use the real paths below.
"""

import json
from json_safe import json_safe
import os
import uuid
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.jsonl")
STATUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_status.json")

TRADE_ANALYTICS_FIELDS = {
    "signal_id",
    "entry_operation_id",
    "entry_order_id",
    "entry_time",
    "candidate_rank",
    "candidate_count",
    "ranking_score",
    "entry_quality_score",
    "entry_quality_detail",
    "entry_context_score",
    "entry_context_detail",
    "confirmation_count",
    "adx_state",
    "adx_current",
    "adx_previous",
    "adx_delta",
    "relative_strength_score",
    "relative_strength_detail",
    "mfe_pct",
    "mae_pct",
}


def record_trade(symbol, direction, qty, entry, exit_price, pnl, result, exchange="NSE",
                  gross_pnl=None, costs=None, analytics=None, log_path=None):
    """
    pnl is the TRUE NET result (costs deducted) -- the authoritative
    field kill-switch/dashboard/stats should use. gross_pnl/costs are
    optional extra detail (default to pnl/0 for backward compat).

    analytics is an optional, reporting-only mapping copied from the
    position that produced this exit.  It carries the durable entry
    operation ID and entry-quality context into the closed-trade record,
    allowing daily reports to join a trade to its exact signal without
    changing any trading decision.
    """
    path = log_path if log_path is not None else LOG_PATH
    record = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "qty": qty,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl,
        "gross_pnl": gross_pnl if gross_pnl is not None else pnl,
        "costs": costs if costs is not None else 0.0,
        "result": result,
    }
    if analytics:
        record.update({
            key: value
            for key, value in analytics.items()
            if key in TRADE_ANALYTICS_FIELDS
            and value is not None
        })
    with open(path, "a") as f:
        f.write(json.dumps(json_safe(record)) + "\n")


def get_trade_history(limit=100, log_path=None):
    path = log_path if log_path is not None else LOG_PATH
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(records))[:limit]


def get_today_summary(log_path=None):
    today = datetime.now().strftime("%Y-%m-%d")
    todays = [r for r in get_trade_history(limit=10000, log_path=log_path) if r["date"] == today]
    return {"count": len(todays), "total_pnl": sum(r["pnl"] for r in todays)}


def save_bot_status(status_list, positions=None, portfolio_summary=None,
                    session_summary=None, health=None, status_path=None):
    """
    Backward-compatible: existing callers passing only status_list are
    completely unaffected. New optional params add the expanded
    institutional-dashboard data (per-position analytics, portfolio/
    session aggregation, health check) as additional top-level keys --
    analytics-only, never read by any trading-decision code.

    Writes atomically: builds the full payload in memory, writes it to
    a same-directory temp file, flushes and fsyncs, then os.replace()s
    the real path -- a reader can never see a partially-written file.
    Temp file is cleaned up even if the write fails partway through.
    Adds snapshot_id (a fresh uuid4 per write) and generated_at (ISO
    timestamp) so every section in one read of the file is guaranteed
    to belong to the same snapshot, not a mix of different writes.
    """
    path = status_path if status_path is not None else STATUS_PATH
    now = datetime.now()
    data = {
        "snapshot_id": str(uuid.uuid4()),
        "generated_at": now.isoformat(),
        "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": status_list,
    }
    if positions is not None:
        data["positions"] = positions
    if portfolio_summary is not None:
        data["portfolio_summary"] = portfolio_summary
    if session_summary is not None:
        data["session_summary"] = session_summary
    if health is not None:
        data["health"] = health

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(json_safe(data), f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def load_bot_status(status_path=None):
    path = status_path if status_path is not None else STATUS_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
