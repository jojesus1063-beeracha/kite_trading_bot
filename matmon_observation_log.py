from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("matmon_observation_log")

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path("runtime/matmon/observations")


def _json_safe(value):
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(k): _json_safe(v)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]

    try:
        return float(value)
    except Exception:
        return str(value)


def observation_path(now: datetime | None = None) -> Path:
    now = now or datetime.now(IST)

    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    return ROOT / f"{now.date().isoformat()}.jsonl"


def append_observation(record: dict) -> bool:
    """
    Best-effort Matmon research persistence.

    IMPORTANT:
    Recording failure must NEVER authorize, reject, resize,
    reverse, or otherwise affect a trade.
    """
    try:
        now = datetime.now(IST)
        path = observation_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = dict(record or {})

        payload.setdefault(
            "recorded_at",
            now.isoformat(),
        )
        payload.setdefault(
            "schema_version",
            1,
        )

        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _json_safe(payload),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

        return True

    except Exception:
        logger.exception(
            "MATMON OBSERVATION RECORDING FAILED "
            "| OBSERVATION_ONLY=True"
        )
        return False
