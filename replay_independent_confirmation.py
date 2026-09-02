#!/usr/bin/env python3

import csv
import json
from datetime import datetime
from pathlib import Path

import config
from costs import net_pnl_for_trade

TRADES = Path(
    "runtime/ema_threshold_replay/trades.csv"
)
EMA_LIMIT = 2.00
RISK_PCT = 0.40
LEVERAGE = 4.0
CAPITAL = float(config.CAPITAL)

AUDIT_FILES = [
    *sorted(Path("validation_events").glob("*.jsonl")),
    Path(
        "runtime/live_combined_audit/"
        "entry_audit.jsonl"
    ),
]


def truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in {
        "true", "1", "yes", "passed"
    }


def timestamp(value):
    if not value:
        return None

    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def load_audits():
    records = []

    for path in AUDIT_FILES:
        if not path.exists():
            continue

        for line in path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = row.get("payload") or {}
            source = payload if payload else row

            symbol = (
                row.get("symbol")
                or payload.get("symbol")
            )
            direction = (
                row.get("final_direction")
                or row.get("direction")
                or payload.get("final_direction")
                or payload.get("direction")
            )

            time_value = (
                row.get("logged_at")
                or row.get("timestamp")
                or row.get("recorded_at")
                or row.get("candle_time")
                or payload.get("logged_at")
                or payload.get("timestamp")
                or payload.get("recorded_at")
                or payload.get("candle_time")
            )

            ts = timestamp(time_value)
            if not symbol or not ts:
                continue

            records.append({
                "symbol": str(symbol).split(":")[-1],
                "direction": direction,
                "timestamp": ts,
                "row": row,
                "source": source,
                "file": str(path),
            })

    return records


def independent_evidence(record, direction):
    row = record["row"]
    source = record["source"]

    candle = (
        row.get("candle_eligibility")
        or source.get("candle_eligibility")
        or {}
    )
    candle_detail = candle.get("detail") or {}

    patterns = (
        candle_detail.get("patterns")
        or source.get("patterns")
        or row.get("patterns")
    )

    price_action = (
        nested(
            row,
            "price_action_confirmation",
            "detail",
        )
        or nested(
            source,
            "price_action_confirmation",
            "detail",
        )
        or candle_detail.get("price_action")
        or source.get("price_action")
    )

    evidence_available = (
        isinstance(patterns, dict)
        or isinstance(price_action, dict)
    )

    if not evidence_available:
        return None, {
            "pattern": False,
            "pullback": False,
            "rejection": False,
        }

    patterns = patterns or {}
    price_action = price_action or {}

    if direction == "BUY":
        directional_patterns = (
            patterns.get("tier1_bullish") or []
        )
    else:
        directional_patterns = (
            patterns.get("tier1_bearish") or []
        )

    pattern_pass = bool(directional_patterns)
    pullback_pass = truthy(
        price_action.get("pullback")
    )
    rejection_pass = truthy(
        price_action.get("rejection_candle")
    )

    return bool(
        pattern_pass
        or pullback_pass
        or rejection_pass
    ), {
        "pattern": pattern_pass,
        "patterns": directional_patterns,
        "pullback": pullback_pass,
        "rejection": rejection_pass,
    }


def load_trades():
    rows = []

    with TRADES.open() as handle:
        for row in csv.DictReader(handle):
            if abs(
                float(row["ema_limit"]) - EMA_LIMIT
            ) > 0.0001:
                continue

            row["timestamp_parsed"] = timestamp(
                row.get("timestamp")
            )
            row["entry"] = float(row["entry"])
            row["exit"] = float(row["exit"])
            row["stop_loss"] = float(
                row["stop_loss"]
            )

            if row["timestamp_parsed"]:
                rows.append(row)

    return sorted(
        rows,
        key=lambda row: row["timestamp_parsed"],
    )


def nearest_audit(trade, audits):
    symbol = str(
        trade.get("symbol", "")
    ).split(":")[-1]
    trade_time = trade["timestamp_parsed"]

    choices = [
        record for record in audits
        if record["symbol"] == symbol
        and record["timestamp"].date()
            == trade_time.date()
    ]

    if not choices:
        return None

    compatible = []
    for record in choices:
        candidate = record["timestamp"]

        # Normalise timezone differences if necessary.
        try:
            difference = abs(
                (candidate - trade_time).total_seconds()
            )
        except TypeError:
            difference = abs(
                (
                    candidate.replace(tzinfo=None)
                    - trade_time.replace(tzinfo=None)
                ).total_seconds()
            )

        if difference <= 15 * 60:
            compatible.append((difference, record))

    if not compatible:
        return None

    return min(
        compatible,
        key=lambda item: item[0],
    )[1]


def current_net(trade):
    entry = trade["entry"]
    stop = trade["stop_loss"]
    risk_per_share = abs(entry - stop)

    if risk_per_share <= 0:
        return None

    risk_amount = CAPITAL * RISK_PCT / 100
    risk_qty = int(risk_amount / risk_per_share)
    margin_qty = int(
        CAPITAL * LEVERAGE / entry
    )
    quantity = min(risk_qty, margin_qty)

    if quantity <= 0:
        return None

    result = net_pnl_for_trade(
        trade["direction"],
        quantity,
        entry,
        trade["exit"],
    )

    return {
        "quantity": quantity,
        "gross": float(result["gross_pnl"]),
        "costs": float(result["costs"]),
        "net": float(result["net_pnl"]),
    }


audits = load_audits()
trades = load_trades()

passed = []
failed = []
unknown = []

print("HISTORICAL INDEPENDENT-CONFIRMATION CHECK")
print("-----------------------------------------")
print("EMA maximum       :", EMA_LIMIT, "ATR")
print("Risk per trade    :", RISK_PCT, "%")
print("Leverage cap      :", LEVERAGE, "x")
print("Historical trades :", len(trades))
print()

for trade in trades:
    record = nearest_audit(trade, audits)

    if record is None:
        status = "UNKNOWN"
        evidence = {}
        unknown.append(trade)
    else:
        result, evidence = independent_evidence(
            record,
            trade["direction"],
        )

        if result is True:
            status = "PASS"
            passed.append(trade)
        elif result is False:
            status = "BLOCK"
            failed.append(trade)
        else:
            status = "UNKNOWN"
            unknown.append(trade)

    pnl = current_net(trade)
    net = pnl["net"] if pnl else 0.0

    print(
        f"{status:7} | "
        f"{trade['timestamp_parsed']} | "
        f"{trade.get('symbol')} | "
        f"{trade['direction']} | "
        f"pattern={evidence.get('pattern')} | "
        f"pullback={evidence.get('pullback')} | "
        f"rejection={evidence.get('rejection')} | "
        f"net=₹{net:.2f}"
    )

print()
print("SUMMARY")
print("-------")
print("Passed new gate :", len(passed))
print("Blocked         :", len(failed))
print("Evidence missing:", len(unknown))

known_pass_net = sum(
    (current_net(row) or {}).get("net", 0.0)
    for row in passed
)
known_wins = sum(
    (current_net(row) or {}).get("net", 0.0) > 0
    for row in passed
)
known_losses = sum(
    (current_net(row) or {}).get("net", 0.0) <= 0
    for row in passed
)

print("Known-pass wins :", known_wins)
print("Known-pass losses:", known_losses)
print("Known-pass net  : ₹%.2f" % known_pass_net)

if unknown:
    print()
    print(
        "WARNING: UNKNOWN trades cannot honestly be "
        "classified because their historical audit "
        "records lack pattern/pullback/rejection data."
    )
