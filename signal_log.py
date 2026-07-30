import json
from json_safe import json_safe
import os
from datetime import date

SIGNAL_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_logs")

def _log_path_for_today():
    return os.path.join(SIGNAL_LOG_DIR, f"signals_{date.today().isoformat()}.jsonl")

def log_signal(record):
    try:
        os.makedirs(SIGNAL_LOG_DIR, exist_ok=True)
        with open(_log_path_for_today(), "a") as f:
            f.write(json.dumps(json_safe(record), default=str) + "\n")
        return True
    except Exception:
        return False

def load_signals_for_date(iso_date):
    path = os.path.join(SIGNAL_LOG_DIR, f"signals_{iso_date}.jsonl")
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
    return records
