from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from validation_log import (
    append_validation_event,
    load_validation_events,
)


passed = 0
failed = 0


def check(name, condition):
    global passed, failed

    if condition:
        passed += 1
        print("PASS:", name)
    else:
        failed += 1
        print("FAIL:", name)


production_dir = Path("validation_events")
production_before = (
    sorted(production_dir.glob("*"))
    if production_dir.exists()
    else None
)

with tempfile.TemporaryDirectory() as temp:
    timestamp = datetime(
        2026,
        8,
        3,
        10,
        15,
        tzinfo=ZoneInfo(
            "Asia/Kolkata"
        ),
    )

    first = append_validation_event(
        "candidate_rejected",
        {
            "symbol": "TEST",
            "direction": "BUY",
            "reason_code": "ENTRY_OVEREXTENDED",
            "ranking_score": 142.5,
            "detail": {
                "atr": Decimal("2.50"),
                "invalid_number": float("nan"),
            },
        },
        log_dir=temp,
        recorded_at=timestamp,
    )

    second = append_validation_event(
        "candidate_accepted",
        {
            "symbol": "TEST2",
            "direction": "SELL",
        },
        log_dir=temp,
        recorded_at=timestamp,
    )

    path = Path(temp) / "2026-08-03.jsonl"

    check(
        "Daily JSONL file created",
        path.exists(),
    )

    check(
        "Ledger file permission is 600",
        oct(path.stat().st_mode & 0o777)
        == "0o600",
    )

    raw_lines = path.read_text(
        encoding="utf-8"
    ).splitlines()

    check(
        "Two append-only rows written",
        len(raw_lines) == 2,
    )

    parsed = [
        json.loads(line)
        for line in raw_lines
    ]

    check(
        "Every row is valid JSON",
        len(parsed) == 2,
    )

    check(
        "Schema version is present",
        first["schema_version"] == 1,
    )

    check(
        "Event IDs are unique",
        first["event_id"]
        != second["event_id"],
    )

    check(
        "IST session date is correct",
        first["session_date"]
        == "2026-08-03",
    )

    check(
        "Nested Decimal is JSON-safe",
        first["payload"]["detail"]["atr"]
        == 2.5,
    )

    check(
        "Non-finite number becomes null",
        first["payload"]["detail"][
            "invalid_number"
        ]
        is None,
    )

    loaded = load_validation_events(
        "2026-08-03",
        log_dir=temp,
    )

    check(
        "Loader returns both events",
        len(loaded) == 2,
    )

    check(
        "Event order is preserved",
        [
            row["event_type"]
            for row in loaded
        ]
        == [
            "candidate_rejected",
            "candidate_accepted",
        ],
    )

try:
    append_validation_event(
        "",
        {},
        log_dir=temp,
    )
except ValueError:
    check(
        "Empty event type is rejected",
        True,
    )
else:
    check(
        "Empty event type is rejected",
        False,
    )

production_after = (
    sorted(production_dir.glob("*"))
    if production_dir.exists()
    else None
)

check(
    "Production ledger was untouched",
    production_before
    == production_after,
)

print()
print(
    f"FINAL RESULTS: "
    f"{passed} passed, "
    f"{failed} failed"
)

if failed:
    raise SystemExit(1)

print()
print(
    "VALIDATION LEDGER TESTS PASSED"
)
