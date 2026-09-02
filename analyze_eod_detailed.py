#!/usr/bin/env python3

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).strftime("%Y-%m-%d")

NEW_LOGIC_FROM = datetime.fromisoformat(
    f"{TODAY}T10:11:16+05:30"
)

AUDIT = Path(
    "runtime/live_combined_audit/entry_audit.jsonl"
)
VALIDATION = Path(
    f"validation_events/{TODAY}.jsonl"
)
HISTORY = Path("trade_history.jsonl")

BODY_LIMIT = 1.50
EMA_LIMIT = 2.00
VWAP_LIMIT = 2.50


def load_jsonl(path):
    rows = []
    malformed = 0

    if not path.exists():
        return rows, malformed

    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        if not line.strip():
            continue

        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1

    return rows, malformed


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None


def percentage(count, denominator):
    return (
        count / denominator * 100
        if denominator else 0.0
    )


def show_segment(title, rows):
    decisions = Counter(
        row.get("decision", "UNKNOWN")
        for row in rows
    )
    stages = Counter(
        row.get("stage", "UNKNOWN")
        for row in rows
    )

    reasons = Counter()
    sole_reasons = Counter()

    for row in rows:
        row_reasons = row.get("reasons") or []
        reasons.update(row_reasons)

        if len(row_reasons) == 1:
            sole_reasons.update(row_reasons)

    selected = [
        row for row in rows
        if row.get("decision") == "SIGNAL_SELECTED"
    ]

    print()
    print(title)
    print("=" * len(title))
    print("Evaluations      :", len(rows))
    print("Signals selected :", len(selected))
    print(
        "Signal rate      :",
        f"{percentage(len(selected), len(rows)):.2f}%",
    )
    print("Decisions        :", dict(decisions))

    print("\nRejected by stage:")
    for stage, count in stages.most_common():
        print(
            f"  {stage}: {count} "
            f"({percentage(count, len(rows)):.2f}%)"
        )

    print("\nOverlapping filter rejections:")
    for reason, count in reasons.most_common(20):
        print(
            f"  {reason}: {count} "
            f"({percentage(count, len(rows)):.2f}%)"
        )

    print("\nTrue sole blockers:")
    if sole_reasons:
        for reason, count in sole_reasons.most_common():
            print(f"  {reason}: {count}")
    else:
        print("  None")

    return selected


audit_rows, malformed_audit = load_jsonl(AUDIT)

today_audit = [
    row for row in audit_rows
    if str(row.get("logged_at", ""))[:10]
       == TODAY
]

before = []
after = []

for row in today_audit:
    logged = parse_time(row.get("logged_at"))

    if logged and logged >= NEW_LOGIC_FROM:
        after.append(row)
    else:
        before.append(row)

print("LIVE BOT — DETAILED END-OF-DAY ANALYSIS")
print("=======================================")
print("Date                  :", TODAY)
print("New logic active from :", NEW_LOGIC_FROM)
print("Audit records today   :", len(today_audit))
print("Malformed audit rows  :", malformed_audit)

selected_before = show_segment(
    "BEFORE INDEPENDENT CONFIRMATION",
    before,
)
selected_after = show_segment(
    "AFTER INDEPENDENT CONFIRMATION",
    after,
)

# ---------------------------------------------------------
# Breakout near misses
# ---------------------------------------------------------
breakout_near = []

for row in today_audit:
    validation = (
        row.get("breakout_validation") or {}
    )

    if validation.get("passed") is True:
        continue

    failures = validation.get("reasons") or []
    metrics = validation.get("metrics") or {}

    # Exactly one failed breakout component.
    if len(failures) == 1:
        breakout_near.append({
            "time": row.get("logged_at"),
            "symbol": row.get("symbol"),
            "direction": row.get("final_direction"),
            "failure": failures[0],
            "metrics": metrics,
        })

print()
print("BREAKOUT NEAR MISSES")
print("====================")
print(
    "Definition: exactly one breakout component failed."
)
print("Count:", len(breakout_near))

for item in breakout_near[:30]:
    m = item["metrics"]

    print(
        item["time"],
        item["symbol"],
        item["direction"],
        "failed=",
        item["failure"],
        "close=",
        m.get("breakout_close"),
        "high=",
        m.get("n_period_high"),
        "low=",
        m.get("n_period_low"),
        "volume=",
        m.get("volume_ratio"),
        "ATR-expansion=",
        m.get("atr_multiplier"),
        "CLV=",
        m.get("clv"),
    )

# ---------------------------------------------------------
# Validation and entry-quality analysis
# ---------------------------------------------------------
validation_rows, malformed_validation = (
    load_jsonl(VALIDATION)
)

today_validation = [
    row for row in validation_rows
    if (
        row.get("session_date") == TODAY
        or str(row.get("recorded_at", ""))[:10]
           == TODAY
    )
]

event_types = Counter(
    row.get("event_type", "UNKNOWN")
    for row in today_validation
)

reason_codes = Counter()
quality_rows = []
symbol_rejections = Counter()

for row in today_validation:
    payload = row.get("payload") or {}
    reason_code = payload.get("reason_code")

    if reason_code:
        reason_codes[reason_code] += 1

    if row.get("event_type") == "candidate_rejected":
        symbol = payload.get("symbol")
        if symbol:
            symbol_rejections[symbol] += 1

    detail = payload.get("entry_quality_detail")
    if isinstance(detail, dict):
        quality_rows.append({
            "recorded_at": row.get("recorded_at"),
            "symbol": payload.get("symbol"),
            "direction": payload.get("direction"),
            "reason_code": reason_code,
            "reason": payload.get("reason"),
            "detail": detail,
        })

print()
print("DOWNSTREAM VALIDATION")
print("=====================")
print("Validation events :", len(today_validation))
print("Malformed rows    :", malformed_validation)
print("Event types       :", dict(event_types))

print("\nReason codes:")
for reason, count in reason_codes.most_common():
    print(
        f"  {reason}: {count} "
        f"({percentage(count, len(today_validation)):.2f}%)"
    )

print("\nMost frequently rejected symbols:")
for symbol, count in symbol_rejections.most_common(15):
    print(f"  {symbol}: {count}")

# ---------------------------------------------------------
# Entry-quality near misses
# ---------------------------------------------------------
quality_near = []

for item in quality_rows:
    detail = item["detail"]

    body = detail.get("signal_body_atr")
    ema = detail.get("ema_distance_atr")
    vwap = detail.get("vwap_distance_atr")

    failed = []

    if body is not None and float(body) > BODY_LIMIT:
        failed.append((
            "BODY",
            float(body),
            BODY_LIMIT,
        ))

    if ema is not None and float(ema) > EMA_LIMIT:
        failed.append((
            "EMA9",
            float(ema),
            EMA_LIMIT,
        ))

    if (
        vwap is not None
        and float(vwap) > VWAP_LIMIT
    ):
        failed.append((
            "VWAP",
            float(vwap),
            VWAP_LIMIT,
        ))

    # Near miss: only one quality rule failed and it
    # exceeded the limit by no more than 20%.
    if len(failed) == 1:
        name, value, limit = failed[0]
        excess_pct = (
            (value - limit) / limit * 100
        )

        if excess_pct <= 20:
            quality_near.append({
                **item,
                "failed_filter": name,
                "value": value,
                "limit": limit,
                "excess_pct": excess_pct,
            })

quality_near.sort(
    key=lambda item: item["excess_pct"]
)

print()
print("ENTRY-QUALITY NEAR MISSES")
print("=========================")
print(
    "Definition: only one quality rule failed, "
    "within 20% of its limit."
)
print("Count:", len(quality_near))

for item in quality_near[:30]:
    print(
        item["recorded_at"],
        item["symbol"],
        item["direction"],
        item["failed_filter"],
        f"value={item['value']:.4f}",
        f"limit={item['limit']:.4f}",
        f"excess={item['excess_pct']:.2f}%",
    )

# ---------------------------------------------------------
# New independent gate: unique and overlapping impact
# ---------------------------------------------------------
independent = (
    "INDEPENDENT_ENTRY_CONFIRMATION_REQUIRED"
)

independent_total = 0
independent_only = 0
independent_plus_breakout = 0

for row in after:
    reasons = row.get("reasons") or []

    if independent not in reasons:
        continue

    independent_total += 1

    other = [
        reason for reason in reasons
        if reason != independent
    ]

    if not other:
        independent_only += 1

    if "BREAKOUT_VALIDATION_FAILED" in other:
        independent_plus_breakout += 1

print()
print("INDEPENDENT CONFIRMATION IMPACT")
print("===============================")
print("Appeared in rejections :", independent_total)
print("Was the only blocker   :", independent_only)
print(
    "Also failed breakout  :",
    independent_plus_breakout,
)
print(
    "Important: only-blocker count represents "
    "otherwise eligible signals stopped solely "
    "by the new gate."
)

# ---------------------------------------------------------
# Selected signals
# ---------------------------------------------------------
print()
print("SIGNALS AFTER NEW LOGIC")
print("=======================")

for row in selected_after:
    candle = row.get("candle_eligibility") or {}
    detail = candle.get("detail") or {}

    print(
        row.get("logged_at"),
        row.get("symbol"),
        row.get("final_direction"),
        "entry=",
        row.get("entry_price"),
        "quality confirmations=",
        detail.get("confirmations"),
    )

# ---------------------------------------------------------
# Completed trades and P&L
# ---------------------------------------------------------
history_rows, malformed_history = load_jsonl(
    HISTORY
)

today_trades = [
    row for row in history_rows
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
print("=====================")
print("Trades     :", len(today_trades))
print("Wins       :", wins)
print("Losses     :", losses)
print(
    "Win rate  :",
    (
        f"{percentage(wins, len(today_trades)):.2f}%"
        if today_trades else "N/A"
    ),
)
print(f"Gross P&L  : ₹{gross:.2f}")
print(f"Costs      : ₹{costs:.2f}")
print(f"Net P&L    : ₹{net:.2f}")
print("Malformed  :", malformed_history)

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
        "gross=₹%.2f" % float(
            row.get("gross_pnl") or 0
        ),
        "costs=₹%.2f" % float(
            row.get("costs") or 0
        ),
        "net=₹%.2f" % float(
            row.get("pnl") or 0
        ),
    )
