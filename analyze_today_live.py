#!/usr/bin/env python3

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

AUDIT = Path(
    "runtime/live_combined_audit/entry_audit.jsonl"
)
HISTORY = Path("trade_history.jsonl")

TODAY = "2026-08-19"
NEW_LOGIC_FROM = datetime.fromisoformat(
    "2026-08-19T10:11:16+05:30"
)


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None


def load_jsonl(path):
    rows = []
    malformed = 0

    if not path.exists():
        return rows, malformed

    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1

    return rows, malformed


def summarize(name, rows):
    decisions = Counter(
        row.get("decision", "UNKNOWN")
        for row in rows
    )
    stages = Counter(
        row.get("stage", "UNKNOWN")
        for row in rows
    )
    reasons = Counter()

    for row in rows:
        reasons.update(row.get("reasons") or [])

    selected = [
        row for row in rows
        if row.get("decision") == "SIGNAL_SELECTED"
    ]

    independent_blocks = sum(
        "INDEPENDENT_ENTRY_CONFIRMATION_REQUIRED"
        in (row.get("reasons") or [])
        for row in rows
    )

    print()
    print(name)
    print("-" * len(name))
    print("Evaluations          :", len(rows))
    print("Decisions            :", dict(decisions))
    print("Signals selected     :", len(selected))
    print("Independent blocks   :", independent_blocks)
    print("Stages               :", dict(stages))

    print("Top rejection reasons:")
    for reason, count in reasons.most_common(12):
        percentage = (
            count / len(rows) * 100
            if rows else 0
        )
        print(
            f"  {reason}: "
            f"{count} ({percentage:.2f}%)"
        )

    if selected:
        print("Selected signals:")
        for row in selected:
            candle = (
                row.get("candle_eligibility")
                or {}
            )
            detail = candle.get("detail") or {}
            confirmations = (
                detail.get("confirmations") or {}
            )

            print(
                " ",
                row.get("logged_at"),
                row.get("symbol"),
                row.get("final_direction"),
                "entry=",
                row.get("entry_price"),
                "confirmations=",
                confirmations,
            )


audit_rows, malformed_audit = load_jsonl(AUDIT)

today_audit = [
    row for row in audit_rows
    if str(row.get("logged_at", ""))[:10]
       == TODAY
]

before = []
after = []

for row in today_audit:
    recorded = parse_time(row.get("logged_at"))

    if recorded and recorded >= NEW_LOGIC_FROM:
        after.append(row)
    else:
        before.append(row)

print("TODAY'S LIVE ANALYSIS")
print("=====================")
print("Date                 :", TODAY)
print("New logic active from:", NEW_LOGIC_FROM.isoformat())
print("Malformed audit rows :", malformed_audit)

summarize("BEFORE NEW LOGIC", before)
summarize("AFTER NEW LOGIC", after)

trade_rows, malformed_trades = load_jsonl(HISTORY)

today_trades = [
    row for row in trade_rows
    if row.get("date") == TODAY
]

wins = sum(
    float(row.get("pnl") or 0) > 0
    for row in today_trades
)
losses = sum(
    float(row.get("pnl") or 0) < 0
    for row in today_trades
)
gross = sum(
    float(row.get("gross_pnl") or 0)
    for row in today_trades
)
costs = sum(
    float(row.get("costs") or 0)
    for row in today_trades
)
net = sum(
    float(row.get("pnl") or 0)
    for row in today_trades
)

print()
print("COMPLETED LIVE TRADES")
print("---------------------")
print("Trades       :", len(today_trades))
print("Wins         :", wins)
print("Losses       :", losses)
print(
    "Win rate    :",
    f"{wins / len(today_trades) * 100:.2f}%"
    if today_trades else "N/A",
)
print(f"Gross P&L    : ₹{gross:.2f}")
print(f"Costs        : ₹{costs:.2f}")
print(f"Net P&L      : ₹{net:.2f}")
print("Malformed rows:", malformed_trades)

for row in today_trades:
    print(
        row.get("time"),
        row.get("symbol"),
        row.get("direction"),
        "qty=",
        row.get("qty"),
        "entry=",
        row.get("entry"),
        "exit=",
        row.get("exit"),
        "result=",
        row.get("result"),
        "net=₹%.2f" % float(
            row.get("pnl") or 0
        ),
    )
