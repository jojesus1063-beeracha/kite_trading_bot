#!/usr/bin/env python3

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

TRADES_FILE = Path(
    "runtime/ema_threshold_replay/trades.csv"
)
VALIDATION_DIR = Path("validation_events")
EMA_LIMIT = 1.75

NUMERIC_FACTORS = {
    "ema_distance": (
        "ema_distance_atr",
    ),
    "body_atr": (
        "signal_body_atr",
    ),
    "vwap_distance": (
        "vwap_distance_atr",
    ),
    "quality_score": (
        "entry_quality_score",
        "quality_score",
    ),
    "entry_context_score": (
        "entry_context_score",
    ),
    "relative_strength": (
        "relative_strength_score",
    ),
    "confirmation_count": (
        "confirmation_count",
    ),
    "adx": (
        "adx_current",
        "adx",
    ),
    "volume_ratio": (
        "volume_ratio",
    ),
    "ranking_score": (
        "ranking_score",
    ),
}

CATEGORICAL_FACTORS = {
    "market_alignment": (
        "market_alignment",
    ),
    "confidence": (
        "confidence",
    ),
    "adx_state": (
        "adx_state",
    ),
}


def parse_time(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None


def minute(value):
    stamp = parse_time(value)

    if stamp:
        return stamp.strftime("%Y-%m-%d %H:%M")

    return str(value or "")[:16]


def flatten(value, prefix=""):
    output = {}

    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            output.update(flatten(item, path))
    elif isinstance(value, list):
        output[prefix] = value
    else:
        output[prefix] = value

    return output


def find_value(flattened, names):
    for name in names:
        exact = [
            value
            for key, value in flattened.items()
            if key == name
        ]

        if exact:
            return exact[0]

        suffix = [
            value
            for key, value in flattened.items()
            if key.endswith("." + name)
        ]

        if suffix:
            return suffix[0]

    return None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


trades = []

with TRADES_FILE.open() as handle:
    for row in csv.DictReader(handle):
        if abs(
            float(row["ema_limit"]) - EMA_LIMIT
        ) > 0.0001:
            continue

        row["outcome"] = (
            "WIN"
            if float(row["net_pnl"]) > 0
            else "LOSS"
        )

        stamp = parse_time(row["timestamp"])

        row["entry_hour"] = (
            stamp.hour
            if stamp
            else None
        )

        trades.append(row)

events = {}

for path in sorted(
    VALIDATION_DIR.glob("*.jsonl")
):
    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue

        payload = event.get("payload") or {}
        signal = payload.get("signal") or payload

        key = (
            event.get("session_date") or path.stem,
            signal.get("symbol")
            or payload.get("symbol"),
            signal.get("direction")
            or payload.get("direction"),
            minute(
                signal.get("timestamp")
                or payload.get("timestamp")
            ),
        )

        events[key] = flatten(payload)

groups = {
    "WIN": [],
    "LOSS": [],
}

for trade in trades:
    key = (
        trade["date"],
        trade["symbol"],
        trade["direction"],
        minute(trade["timestamp"]),
    )

    flattened = events.get(key, {})
    factors = {
        "date": trade["date"],
        "symbol": trade["symbol"],
        "direction": trade["direction"],
        "outcome": trade["outcome"],
        "net_pnl": number(trade["net_pnl"]),
        "exit_reason": trade["exit_reason"],
        "entry_hour": trade["entry_hour"],
        "ema_distance": number(trade["ema"]),
        "body_atr": number(trade["body"]),
        "vwap_distance": number(trade["vwap"]),
    }

    for factor, names in NUMERIC_FACTORS.items():
        if factors.get(factor) is None:
            factors[factor] = number(
                find_value(flattened, names)
            )

    for factor, names in CATEGORICAL_FACTORS.items():
        factors[factor] = find_value(
            flattened,
            names,
        )

    groups[trade["outcome"]].append(factors)

print("INDIVIDUAL TRADES")
print("=================")

for outcome in ("WIN", "LOSS"):
    for row in groups[outcome]:
        print(
            f"{outcome:4} | {row['date']} | "
            f"{row['symbol']:12} | "
            f"{row['direction']:4} | "
            f"EMA={row['ema_distance']} | "
            f"BODY={row['body_atr']} | "
            f"VWAP={row['vwap_distance']} | "
            f"ADX={row['adx']} | "
            f"RS={row['relative_strength']} | "
            f"CONF={row['confirmation_count']} | "
            f"MARKET={row['market_alignment']} | "
            f"EXIT={row['exit_reason']} | "
            f"PNL={row['net_pnl']:.2f}"
        )

print()
print("GROUP COMPARISON")
print("================")

comparison_factors = (
    "ema_distance",
    "body_atr",
    "vwap_distance",
    "quality_score",
    "entry_context_score",
    "relative_strength",
    "confirmation_count",
    "adx",
    "volume_ratio",
    "ranking_score",
    "entry_hour",
)

for factor in comparison_factors:
    parts = []

    for outcome in ("WIN", "LOSS"):
        values = [
            row[factor]
            for row in groups[outcome]
            if isinstance(
                row.get(factor),
                (int, float),
            )
        ]

        if values:
            parts.append(
                f"{outcome}: "
                f"avg={mean(values):.4f}, "
                f"min={min(values):.4f}, "
                f"max={max(values):.4f}, "
                f"n={len(values)}"
            )
        else:
            parts.append(
                f"{outcome}: unavailable"
            )

    print(f"{factor:24} | " + " | ".join(parts))

print()
print("CATEGORICAL COMPARISON")
print("======================")

for factor in (
    "direction",
    "exit_reason",
    "market_alignment",
    "confidence",
    "adx_state",
):
    print(f"\n{factor}:")

    for outcome in ("WIN", "LOSS"):
        counts = Counter(
            str(row.get(factor))
            for row in groups[outcome]
        )

        print(
            f"  {outcome}: {dict(counts)}"
        )

print()
print("TOTALS")
print("======")
print("Wins  :", len(groups["WIN"]))
print("Losses:", len(groups["LOSS"]))
print(
    "Net P&L: ₹%.2f"
    % sum(
        row["net_pnl"]
        for outcome in groups.values()
        for row in outcome
    )
)
