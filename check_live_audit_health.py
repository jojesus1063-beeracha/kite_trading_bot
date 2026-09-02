#!/usr/bin/env python3

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

AUDIT = Path(
    "runtime/live_combined_audit/entry_audit.jsonl"
)
MAX_RECORDS = 1000


def first_value(*values):
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def classify(row):
    payload = row.get("payload") or {}

    explicit = first_value(
        row.get("event_type"),
        row.get("event"),
        row.get("decision"),
        row.get("status"),
        payload.get("event_type"),
        payload.get("event"),
        payload.get("decision"),
        payload.get("status"),
    )

    if explicit:
        return str(explicit)

    reasons = first_value(
        row.get("reasons"),
        row.get("rejection_reasons"),
        payload.get("reasons"),
        payload.get("rejection_reasons"),
    )

    accepted = first_value(
        row.get("accepted"),
        row.get("passed"),
        row.get("eligible"),
        payload.get("accepted"),
        payload.get("passed"),
        payload.get("eligible"),
    )

    if accepted is True:
        return "ENTRY_ACCEPTED"

    if reasons:
        return "ENTRY_REJECTED"

    # These audit rows are entry evaluations even when they do not
    # contain a separately named event field.
    return "ENTRY_EVALUATION"


def rejection_reasons(row):
    payload = row.get("payload") or {}

    values = first_value(
        row.get("reasons"),
        row.get("rejection_reasons"),
        payload.get("reasons"),
        payload.get("rejection_reasons"),
    )

    if not values:
        return []

    if isinstance(values, str):
        return [values]

    if isinstance(values, list):
        return [str(value) for value in values]

    return [str(values)]


if not AUDIT.exists():
    raise SystemExit(f"ERROR: Audit file missing: {AUDIT}")

lines = [
    line for line in AUDIT.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()
    if line.strip()
]

rows = []
malformed = 0

for line in lines[-MAX_RECORDS:]:
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        malformed += 1

events = Counter(classify(row) for row in rows)
reasons = Counter()

for row in rows:
    reasons.update(rejection_reasons(row))

modified = datetime.fromtimestamp(
    AUDIT.stat().st_mtime
).astimezone()

last = rows[-1] if rows else {}
payload = last.get("payload") or {}

last_timestamp = first_value(
    last.get("logged_at"),
    last.get("timestamp"),
    last.get("recorded_at"),
    payload.get("logged_at"),
    payload.get("timestamp"),
)

last_symbol = first_value(
    last.get("symbol"),
    payload.get("symbol"),
)

print("LIVE AUDIT HEALTH")
print("-----------------")
print("File             :", AUDIT.resolve())
print("Last modified    :", modified.isoformat())
print("Total records    :", len(lines))
print("Recent parsed    :", len(rows))
print("Malformed recent :", malformed)
print("Event summary    :", dict(events))
print("Last timestamp   :", last_timestamp)
print("Last symbol      :", last_symbol)

print("\nTop rejection reasons:")
if reasons:
    for reason, count in reasons.most_common(15):
        print(f"  {reason}: {count}")
else:
    print("  No rejection-reason field found.")

print("\nDetected record fields:")
print(" ", sorted(last.keys()) if last else [])
