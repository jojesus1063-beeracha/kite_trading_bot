"""
Simple append-only trade history log, shared between main.py (writes
each closed trade) and configure_app.py (reads it for the dashboard).
"""

import json
import os
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.jsonl")


def record_trade(symbol, direction, qty, entry, exit_price, pnl, result):
    """result should be 'target', 'stop', or 'square_off'."""
    record = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol,
        "direction": direction,
        "qty": qty,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl,
        "result": result,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


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
