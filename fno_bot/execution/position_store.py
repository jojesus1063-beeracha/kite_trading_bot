"""
Persists the F&O bot's open option position(s) to disk, so a crash,
restart, or VM reboot doesn't lose track of what's actually live on
the broker account (spec #27, crash recovery).

Same pattern as the equity bot's position_store.py, duplicated (not
imported) with its own path -- see architecture review Section B for
why. MIS/intraday options positions can't survive overnight either,
so a saved file from a previous day is treated as stale and discarded.
"""

import json
import os
from datetime import datetime

POSITIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_open_positions.json")


def save_positions(open_positions: dict, positions_path=None):
    """Overwrites the saved state, atomically (temp file + rename)."""
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
    """Returns the saved open_positions dict, or {} if nothing to
    restore (no file, corrupt file, or the file is from a previous
    day and therefore stale -- intraday options never carry overnight)."""
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
    """Called once the trading day is fully wound down, so tomorrow starts clean."""
    path = positions_path if positions_path is not None else POSITIONS_PATH
    if os.path.exists(path):
        os.remove(path)
