"""
Simple append-only trade history log, shared between main.py (writes
each closed trade) and configure_app.py (reads it for the dashboard).
"""

import json
from json_safe import json_safe
import os
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.jsonl")


def record_trade(symbol, direction, qty, entry, exit_price, pnl, result, exchange="NSE",
                  gross_pnl=None, costs=None):
    """
    pnl is the TRUE NET result (costs deducted) -- the authoritative
    field kill-switch/dashboard/stats should use. gross_pnl/costs are
    optional extra detail (default to pnl/0 for backward compat).
    """
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
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(json_safe(record)) + "\n")


def get_trade_history(limit=100):
    if not os.path.exists(LOG_PATH):
        return []
    records = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(records))[:limit]


def get_today_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    todays = [r for r in get_trade_history(limit=10000) if r["date"] == today]
    return {"count": len(todays), "total_pnl": sum(r["pnl"] for r in todays)}

STATUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_status.json")

def save_bot_status(status_list, positions=None, portfolio_summary=None,
                    session_summary=None, health=None):
    """
    Backward-compatible: existing callers passing only status_list are
    completely unaffected. New optional params add the expanded
    institutional-dashboard data (per-position analytics, portfolio/
    session aggregation, health check) as additional top-level keys --
    analytics-only, never read by any trading-decision code.
    """
    from json_safe import json_safe
    data = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    with open(STATUS_PATH, "w") as f:
        json.dump(json_safe(data), f, indent=2, default=str)

def load_bot_status():
    if not os.path.exists(STATUS_PATH):
        return None
    with open(STATUS_PATH) as f:
        return json.load(f)
