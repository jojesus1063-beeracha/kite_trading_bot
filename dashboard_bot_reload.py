"""Apply saved dashboard configuration to the running trading bot."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
STATUS_PATH = (
    PROJECT_DIR
    / "runtime"
    / "dashboard_apply_status.json"
)

COMMAND = [
    "/usr/bin/sudo",
    "-n",
    "/usr/local/sbin/kitebot-apply-config-restart",
]


def write_status(payload: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temporary_name = tempfile.mkstemp(
        prefix=".dashboard-apply-status.",
        suffix=".tmp",
        dir=str(STATUS_PATH.parent),
    )

    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(
                payload,
                handle,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            temporary_name,
            STATUS_PATH,
        )
    except Exception:
        Path(temporary_name).unlink(
            missing_ok=True,
        )
        raise


def apply_saved_config() -> tuple[bool, str]:
    """Restart the running bot after dashboard settings are saved."""

    try:
        result = subprocess.run(
            COMMAND,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

        output = (
            result.stdout.strip()
            or result.stderr.strip()
            or f"Helper returned code {result.returncode}"
        )

        applied = result.returncode == 0

        write_status(
            {
                "generated_at": (
                    datetime.now()
                    .astimezone()
                    .isoformat()
                ),
                "applied": applied,
                "return_code": result.returncode,
                "message": output,
            }
        )

        if applied:
            LOGGER.info(
                "Dashboard configuration applied: %s",
                output,
            )
        else:
            LOGGER.warning(
                "Dashboard configuration saved but not "
                "immediately applied: %s",
                output,
            )

        return applied, output

    except Exception as exc:
        message = (
            "Dashboard configuration was saved, but the "
            f"automatic bot restart failed: {exc}"
        )

        LOGGER.exception(message)

        write_status(
            {
                "generated_at": (
                    datetime.now()
                    .astimezone()
                    .isoformat()
                ),
                "applied": False,
                "return_code": None,
                "message": message,
            }
        )

        return False, message
