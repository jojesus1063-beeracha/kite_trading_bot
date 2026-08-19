"""
Append-only closed-trade history + atomic bot-status snapshot for the
F&O bot -- same pattern as the equity bot's trade_log.py (append-only
JSONL, atomic-write status file), duplicated with F&O-scoped paths
rather than imported, per the isolation rationale in the architecture
review Section B. A reporting failure here must never interrupt
trading (spec #31) -- every write is caught and reported via return
value, never raised into the caller.
"""
import json
import os
import uuid
import logging
from datetime import datetime

from fno_bot.json_safe import json_safe
from fno_bot.reporting.costs import net_pnl_for_trade

logger = logging.getLogger("fno.trade_log")

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_trade_history.jsonl")
STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_bot_status.json")


def record_trade(*, underlying, strike, option_type, direction, quantity,
                  entry_price, exit_price, mode, exit_reason,
                  mfe_pct=None, mae_pct=None, log_path=None) -> bool:
    """
    Records ONE closed trade. Computes gross/net P&L via
    reporting/costs.py (spec #19: never evaluate on gross P&L alone).
    Returns True/False rather than raising -- a log-write failure must
    never propagate into position-management code (spec #31).
    """
    path = log_path if log_path is not None else LOG_PATH
    try:
        pnl = net_pnl_for_trade(quantity, entry_price, exit_price)
        record = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "underlying": underlying, "strike": strike, "option_type": option_type,
            "direction": direction, "quantity": quantity,
            "entry_price": entry_price, "exit_price": exit_price,
            "gross_pnl": pnl["gross_pnl"], "costs": pnl["costs"], "net_pnl": pnl["net_pnl"],
            "mode": mode, "exit_reason": exit_reason,
            "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "result": "WIN" if pnl["net_pnl"] > 0 else "LOSS",
        }
        with open(path, "a") as f:
            f.write(json.dumps(json_safe(record)) + "\n")
        return True
    except Exception as e:
        logger.error(f"record_trade failed (trade data preserved in memory, not persisted): {e}")
        return False


def get_trade_history(limit=100, log_path=None) -> list:
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


def get_today_summary(log_path=None) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    todays = [r for r in get_trade_history(limit=10000, log_path=log_path) if r["date"] == today]
    wins = [r for r in todays if r["net_pnl"] > 0]
    losses = [r for r in todays if r["net_pnl"] <= 0]
    return {
        "count": len(todays),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": (len(wins) / len(todays) * 100) if todays else None,
        "total_gross_pnl": sum(r["gross_pnl"] for r in todays),
        "total_costs": sum(r["costs"] for r in todays),
        "total_net_pnl": sum(r["net_pnl"] for r in todays),
    }


def save_bot_status(*, state, mode, underlying, session_summary=None, health=None, status_path=None) -> bool:
    path = status_path if status_path is not None else STATUS_PATH
    try:
        now = datetime.now()
        data = {
            "snapshot_id": str(uuid.uuid4()), "generated_at": now.isoformat(),
            "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
            "state": state, "mode": mode, "underlying": underlying,
            "session_summary": session_summary, "health": health,
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(json_safe(data), f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.error(f"save_bot_status failed: {e}")
        return False


def load_bot_status(status_path=None):
    path = status_path if status_path is not None else STATUS_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
