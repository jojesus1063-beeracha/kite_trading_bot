"""
Append-only JSONL audit log for the F&O opening scalper (spec #21).

Every important lifecycle event gets one line, with enough structured
detail to reconstruct any trade after the fact. Never raises into the
caller's trading path -- a logging failure here must never block or
corrupt position management (spec #31), so write failures are caught
and reported via the return value, not propagated.

One file per trading day (matches equity bot's signal_logs/ pattern),
under fno_bot/audit_logs/events_<date>.jsonl.
"""
import json
import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.json_safe import json_safe

logger = logging.getLogger("fno.audit")

IST = ZoneInfo("Asia/Kolkata")

AUDIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_logs")

# The full event vocabulary from spec #21. Not an enforced enum at
# call sites (new event types may be added), but every event listed
# here is expected to occur somewhere in a normal trading session.
EVENT_TYPES = [
    "BOT_START", "AUTH_OK", "INSTRUMENT_MASTER_LOADED",
    "WEBSOCKET_CONNECTING", "WEBSOCKET_READY", "SUBSCRIPTION_OK",
    "FIRST_UNDERLYING_TICK", "FIRST_CE_TICK", "FIRST_PE_TICK",
    "STRIKE_SELECTED", "SIGNAL_EVALUATED", "SIGNAL_REJECTED",
    "ENTRY_ATTEMPT", "ENTRY_SUBMITTED", "ENTRY_PARTIAL", "ENTRY_FILLED",
    "ENTRY_ABORTED", "MONITORING_STARTED", "TARGET_TRIGGER",
    "STOP_TRIGGER", "TIME_STOP_TRIGGER", "SIGNAL_INVALIDATION",
    "EXIT_SUBMITTED", "EXIT_PARTIAL", "EXIT_FILLED", "TRADE_COMPLETE",
    "DAILY_LIMIT_TRIGGER", "ERROR", "BOT_STOP",
]


def _log_path_for_today():
    return os.path.join(AUDIT_DIR, f"events_{datetime.now(IST).date().isoformat()}.jsonl")


def log_event(event: str, monotonic_start: float = None, **fields) -> bool:
    """
    Appends one audit record. `event` should normally be one of
    EVENT_TYPES (not enforced, to avoid blocking a genuinely new event
    type from being logged). `fields` is arbitrary structured detail
    (symbol, strike, prices, latency, etc).

    timestamp_ist: wall-clock IST timestamp for audit/reconstruction.
    latency_ms: if monotonic_start is given, computed as high-resolution
    elapsed time since that reference (spec #3: monotonic for latency,
    wall-clock IST for audit records -- both are recorded here).

    Returns True on success, False on failure (never raises) -- a
    reporting/audit failure must never interrupt position management.
    """
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        record = {
            "timestamp_ist": datetime.now(IST).isoformat(),
            "event": event,
        }
        if monotonic_start is not None:
            record["latency_ms"] = round((time.monotonic() - monotonic_start) * 1000, 3)
        record.update(fields)
        with open(_log_path_for_today(), "a") as f:
            f.write(json.dumps(json_safe(record), default=str) + "\n")
        return True
    except Exception as e:
        logger.error(f"audit log_event failed for event={event}: {e}")
        return False


def load_events_for_date(iso_date: str):
    path = os.path.join(AUDIT_DIR, f"events_{iso_date}.jsonl")
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
