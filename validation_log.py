"""
Append-only paper-validation event ledger.

This module records observations and decisions only. It does not
generate signals, change rankings, alter risk settings or place orders.

Records are stored by Indian market session date:

    validation_events/YYYY-MM-DD.jsonl
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
SCHEMA_VERSION = 1
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "validation_events"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(
        value,
        (str, bool, int),
    ):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)

        return value.astimezone(IST).isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if is_dataclass(value):
        return _json_safe(asdict(value))

    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item)
            for item in value
        ]

    item_method = getattr(value, "item", None)

    if callable(item_method):
        try:
            return _json_safe(item_method())
        except Exception:
            pass

    return str(value)


def append_validation_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    log_dir: str | Path | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """
    Append one immutable JSON event and return the written record.
    """

    event_type = str(event_type).strip()

    if not event_type:
        raise ValueError(
            "event_type must be non-empty"
        )

    if not isinstance(payload, dict):
        raise TypeError(
            "payload must be a dictionary"
        )

    timestamp = recorded_at or datetime.now(IST)

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)

    timestamp = timestamp.astimezone(IST)

    record = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid4()),
        "event_type": event_type,
        "recorded_at": timestamp.isoformat(),
        "session_date": timestamp.date().isoformat(),
        "payload": _json_safe(payload),
    }

    directory = Path(
        log_dir or DEFAULT_LOG_DIR
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    try:
        directory.chmod(0o700)
    except OSError:
        pass

    path = directory / (
        f"{record['session_date']}.jsonl"
    )

    encoded = (
        json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND,
        0o600,
    )

    try:
        with os.fdopen(fd, "ab", closefd=True) as handle:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX,
            )

            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_UN,
            )
    except Exception:
        raise

    try:
        path.chmod(0o600)
    except OSError:
        pass

    return record


def load_validation_events(
    session_date: str,
    *,
    log_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    directory = Path(
        log_dir or DEFAULT_LOG_DIR
    )

    path = directory / (
        f"{session_date}.jsonl"
    )

    if not path.exists():
        return []

    rows = []

    for raw_line in path.read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.strip()

        if not line:
            continue

        rows.append(json.loads(line))

    return rows
