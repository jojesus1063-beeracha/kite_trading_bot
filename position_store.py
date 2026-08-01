"""
Persists the bot's open positions to disk, so a crash, restart, or
VM reboot doesn't lose track of what's actually live on your broker
account. Without this, an in-memory-only crash mid-day means the bot
"forgets" it has an open position -- its stop-loss and target stop
being watched entirely, even though the position is still real on
Zerodha.

MIS (intraday) positions can't survive overnight -- brokers square
them off automatically -- so a saved file from a previous day is
treated as stale and discarded rather than reloaded.

Every function accepts an optional positions_path override so tests
can inject a temp-directory path directly rather than monkeypatching
the module global -- production callers never pass this, so they're
completely unaffected.
"""

import json
import os
from datetime import datetime

POSITIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "open_positions.json")


def save_positions(open_positions: dict, positions_path=None):
    """Overwrites the saved state. Called after every position change
    (new entry, or a position closing) so the file is never more than
    one trade-cycle out of date. Writes atomically (via a temp file +
    rename) so a crash mid-write can't corrupt the file."""
    path = positions_path if positions_path is not None else POSITIONS_PATH
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "positions": open_positions,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)


def load_positions(positions_path=None) -> dict:
    """Returns the saved open_positions dict, or {} if there's nothing
    to restore (no file, corrupt file, or the file is from a previous
    day and therefore stale)."""
    path = positions_path if positions_path is not None else POSITIONS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    saved_date = data.get("date")
    today = datetime.now().strftime("%Y-%m-%d")
    if saved_date != today:
        return {}

    return data.get("positions", {})


def clear_positions(positions_path=None):
    """Called once the trading day is fully wound down (after the
    force square-off), so tomorrow starts clean."""
    path = positions_path if positions_path is not None else POSITIONS_PATH
    if os.path.exists(path):
        os.remove(path)
