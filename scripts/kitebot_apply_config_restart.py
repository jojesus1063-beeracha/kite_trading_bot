#!/usr/bin/env python3
"""Safely apply dashboard configuration by restarting kitebot.service."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_DIR = Path("/home/ubuntu/kite_trading_bot")
CONFIG_PATH = PROJECT_DIR / "user_config.json"
POSITIONS_PATH = PROJECT_DIR / "open_positions.json"
PENDING_ORDERS_PATH = PROJECT_DIR / "pending_orders.json"
BOT_SERVICE = "kitebot.service"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot read {path.name}: {exc}"
        ) from exc


def extract_open_positions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        nested = payload.get("positions")

        if isinstance(nested, (dict, list)):
            payload = nested

    if isinstance(payload, dict):
        records = [
            value
            for value in payload.values()
            if isinstance(value, dict)
        ]
    elif isinstance(payload, list):
        records = [
            value
            for value in payload
            if isinstance(value, dict)
        ]
    else:
        records = []

    open_records = []

    for record in records:
        try:
            quantity = int(
                record.get(
                    "quantity",
                    record.get("qty", 0),
                )
                or 0
            )
        except (TypeError, ValueError):
            quantity = 0

        if quantity != 0:
            open_records.append(record)

    return open_records


def has_unresolved_pending_orders(payload: Any) -> bool:
    if isinstance(payload, dict):
        nested = payload.get("orders")

        if isinstance(nested, (dict, list)):
            payload = nested

    if isinstance(payload, dict):
        records = [
            value
            for value in payload.values()
            if isinstance(value, dict)
        ]
    elif isinstance(payload, list):
        records = [
            value
            for value in payload
            if isinstance(value, dict)
        ]
    else:
        records = []

    for record in records:
        resolved = record.get("resolved")

        if resolved is False:
            return True

        status = str(
            record.get("status", "")
        ).upper()

        if status in {
            "UNRESOLVED",
            "TIMEOUT",
            "UNKNOWN",
            "SUBMISSION_UNCERTAIN",
            "ENTRY_CONFIRMATION_PENDING",
            "EXIT_CONFIRMATION_PENDING",
        }:
            return True

    return False


def service_is_active() -> bool:
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            BOT_SERVICE,
        ],
        check=False,
    )

    return result.returncode == 0


def main() -> int:
    config = load_json(CONFIG_PATH, {})

    if not isinstance(config, dict):
        print(
            "Configuration was not applied: "
            "user_config.json is not a JSON object"
        )
        return 64

    paper_mode = config.get("paper_trading") is True

    # Saving settings must never unexpectedly start a bot that
    # the user deliberately stopped.
    if not service_is_active():
        print(
            "Settings saved. Trading bot is inactive, "
            "so it was not started. The settings will apply "
            "when the bot is next started."
        )
        return 76

    # Restarting a live bot with open positions would briefly
    # remove its software-only stop monitoring.
    if not paper_mode:
        positions = extract_open_positions(
            load_json(POSITIONS_PATH, {})
        )

        pending = has_unresolved_pending_orders(
            load_json(PENDING_ORDERS_PATH, {})
        )

        if positions or pending:
            print(
                "Settings saved, but automatic restart was "
                "deferred because live positions or unresolved "
                "orders are present."
            )
            return 75

    subprocess.run(
        [
            "/usr/bin/systemctl",
            "restart",
            BOT_SERVICE,
        ],
        check=True,
    )

    verification = subprocess.run(
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            BOT_SERVICE,
        ],
        check=False,
    )

    if verification.returncode != 0:
        print(
            "Configuration was saved, but kitebot.service "
            "did not become active after restart."
        )
        return 1

    mode = "PAPER" if paper_mode else "LIVE"

    print(
        f"Settings saved and applied. "
        f"kitebot.service restarted in {mode} mode."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
