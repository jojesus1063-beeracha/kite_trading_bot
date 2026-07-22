"""
Persists the bot's open positions to disk, so a crash, restart, or
VM reboot doesn't lose track of what's actually live on your broker
account. Without this, an in-memory-only crash mid-day means the bot
"forgets" it has an open position — its stop-loss and target stop
being watched entirely, even though the position is still real on
Zerodha.

MIS (intraday) positions can't survive overnight — brokers square
them off automatically — so a saved file from a previous day is
treated as stale and discarded rather than reloaded.
"""

import json
import os
from datetime import datetime

POSITIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "open_positions.json")


def save_positions(open_positions: dict):
    """Overwrites the saved state. Called after every position change
    (new entry, or a position closing) so the file is never more than
    one trade-cycle out of date. Writes atomically (via a temp file +
    rename) so a crash mid-write can't corrupt the file."""
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "positions": open_positions,
    }
    tmp_path = POSITIONS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, POSITIONS_PATH)


def load_positions() -> dict:
    """Returns the saved open_positions dict, or {} if there's nothing
    to restore (no file, corrupt file, or the file is from a previous
    day and therefore stale)."""
    if not os.path.exists(POSITIONS_PATH):
        return {}
    try:
        with open(POSITIONS_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    saved_date = data.get("date")
    today = datetime.now().strftime("%Y-%m-%d")
    if saved_date != today:
        return {}

    return data.get("positions", {})


def clear_positions():
    """Called once the trading day is fully wound down (after the
    force square-off), so tomorrow starts clean."""
    if os.path.exists(POSITIONS_PATH):
        os.remove(POSITIONS_PATH)
