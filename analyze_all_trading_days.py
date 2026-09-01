#!/usr/bin/env python3

import argparse
import ast
import csv
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
QUALITY_RE = re.compile(r"poor entry location.*?detail=(\{.*\})\s*$")


def as_number(value):
    try:
        value = float(value)
        return value if value == value else None
    except (TypeError, ValueError):
        return None


def first_number(*values):
    for value in values:
        value = as_number(value)
        if value is not None:
            return value
    return None


def get_date(value):
    match = DATE_RE.search(str(value or ""))
    return match.group(1) if match else None


def nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def read_jsonl(path):
    if not path.exists():
        print(f"Warning: file not found: {path}")
        return

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row
            except json.JSONDecodeError:
                print(f"Warning: invalid JSON ignored at {path}:{line_number}")


def fresh_day():
    return {
        "evaluations": 0,
        "candidates": 0,
        "quality_rows": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "pnl": 0.0,
        "directions": Counter(),
        "reasons": Counter(),
        "quality_failures": Counter(),
        "audit_values": defaultdict(list),
        "quality_values": defaultdict(list),
    }


def calculate_audit_metrics(row):
    detail = nested(row, "candle_eligibility", "detail") or {}
    movement = detail.get("cost_aware_movement") or {}

    candle = (
        row.get("entry_last_completed_candle")
        or row.get("last_completed_candle")
        or {}
    )

    atr = first_number(
        movement.get("recent_true_range"),
        detail.get("atr"),
        row.get("atr"),
        nested(row, "breakout_validation", "metrics", "atr_14"),
    )

    close = first_number(
        candle.get("close"),
        row.get("entry_price"),
        row.get("price"),
    )
    open_price = as_number(candle.get("open"))

    ema9 = first_number(
        row.get("ema9"),
        nested(row, "indicators", "ema9"),
    )

    vwap = first_number(
        detail.get("vwap"),
        row.get("vwap"),
        nested(row, "indicators", "vwap"),
    )

    return {
        "atr": atr,
        "adx": first_number(
            row.get("adx"),
            nested(row, "indicators", "adx"),
        ),
        "volume_ratio": first_number(
            detail.get("volume_ratio"),
            row.get("volume_ratio"),
        ),
        "signal_body_atr": (
            abs(close - open_price) / atr
            if close is not None and open_price is not None and atr
            else None
        ),
        "ema_distance_atr": (
            abs(close - ema9) / atr
            if close is not None and ema9 is not None and atr
            else None
        ),
        "vwap_distance_atr": (
            abs(close - vwap) / atr
            if close is not None and vwap is not None and atr
            else None
        ),
    }


def parse_audit(path, days):
    for row in read_jsonl(path):
        event = row.get("event")

        if event not in (None, "ENTRY_EVALUATION") and "decision" not in row:
            continue

        day = get_date(
            row.get("logged_at")
            or row.get("timestamp")
            or row.get("time")
        )

        if not day:
            continue

        stats = days[day]
        stats["evaluations"] += 1

        reasons = row.get("reasons") or row.get("reason") or []

        if isinstance(reasons, str):
            reasons = [reasons]

        for reason in reasons:
            stats["reasons"][str(reason)] += 1

        metrics = calculate_audit_metrics(row)

        for name, value in metrics.items():
            if value is not None:
                stats["audit_values"][name].append(value)

        decision = str(row.get("decision") or "").upper()
        stage = str(row.get("stage") or "").upper()

        if (
            decision in {"SELECTED", "PASS", "ACCEPT", "CANDIDATE"}
            or stage in {"SELECTED", "CANDIDATE", "ENTRY_QUALITY"}
        ):
            stats["candidates"] += 1

            direction = str(
                row.get("direction")
                or row.get("side")
                or "UNKNOWN"
            ).upper()

            stats["directions"][direction] += 1


def get_journal(service, journal_file=None):
    if journal_file:
        path = Path(journal_file)

        if not path.exists():
            print(f"Warning: journal file not found: {path}")
            return ""

        return path.read_text(encoding="utf-8", errors="replace")

    result = subprocess.run(
        [
            "journalctl",
            "-u",
            service,
            "--no-pager",
            "-o",
            "short-iso",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(f"Warning: journalctl failed: {result.stderr.strip()}")
        return ""

    return result.stdout


def parse_journal(text, days):
    for line in text.splitlines():
        day = get_date(line)
        match = QUALITY_RE.search(line)

        if not day or not match:
            continue

        try:
            detail = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue

        if not isinstance(detail, dict):
            continue

        stats = days[day]
        stats["quality_rows"] += 1

        failures = detail.get("failures") or []

        for failure in failures:
            stats["quality_failures"][str(failure)] += 1

        for field in (
            "atr",
            "signal_body_atr",
            "ema_distance_atr",
            "vwap_distance_atr",
        ):
            value = as_number(detail.get(field))

            if value is not None:
                stats["quality_values"][field].append(value)


def parse_trades(path, days):
    grouped_trades = defaultdict(list)

    for index, row in enumerate(read_jsonl(path)):
        day = get_date(
            row.get("exit_time")
            or row.get("timestamp")
            or row.get("entry_time")
        )

        if not day:
            continue

        trade_id = str(
            row.get("signal_id")
            or row.get("trade_id")
            or f"row-{index}"
        )

        grouped_trades[(day, trade_id)].append(row)

    for (day, _), rows in grouped_trades.items():
        pnl_values = [as_number(row.get("net_pnl")) for row in rows]

        if not any(value is not None for value in pnl_values):
            pnl_values = [as_number(row.get("pnl")) for row in rows]

        pnl = sum(value for value in pnl_values if value is not None)

        stats = days[day]
        stats["trades"] += 1
        stats["pnl"] += pnl

        if pnl > 0:
            stats["wins"] += 1
        elif pnl < 0:
            stats["losses"] += 1
        else:
            stats["flat"] += 1


def average(values):
    return round(mean(values), 4) if values else None


def write_csv(path, rows):
    if not rows:
        return

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Analyse every available bot trading day."
    )

    parser.add_argument(
        "--audit",
        type=Path,
        default=Path(
            "runtime/live_combined_audit/entry_audit.jsonl"
        ),
    )

    parser.add_argument(
        "--trades",
        type=Path,
        default=Path("trade_history.jsonl"),
    )

    parser.add_argument(
        "--service",
        default="kitebot-live-combined.service",
    )

    parser.add_argument(
        "--journal-file",
        help="Optional exported journal file",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime/all_days_analysis"),
    )

    args = parser.parse_args()

    days = defaultdict(fresh_day)

    parse_audit(args.audit, days)
    parse_journal(
        get_journal(args.service, args.journal_file),
        days,
    )
    parse_trades(args.trades, days)

    if not days:
        print("No trading records found.")
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    daily_rows = []
    rejection_rows = []
    overall_values = defaultdict(list)
    totals = Counter()

    for day in sorted(days):
        stats = days[day]
        exact = stats["quality_values"]
        audit = stats["audit_values"]

        def preferred(field):
            return exact[field] or audit[field]

        daily_rows.append({
            "date": day,
            "evaluations": stats["evaluations"],
            "candidates": stats["candidates"],
            "buy_candidates": stats["directions"]["BUY"],
            "sell_candidates": stats["directions"]["SELL"],
            "entry_quality_rows": stats["quality_rows"],
            "journal_quality_available": bool(
                stats["quality_rows"]
            ),
            "avg_atr": average(preferred("atr")),
            "avg_body_atr": average(
                preferred("signal_body_atr")
            ),
            "avg_ema9_distance_atr": average(
                preferred("ema_distance_atr")
            ),
            "avg_vwap_distance_atr": average(
                preferred("vwap_distance_atr")
            ),
            "avg_adx": average(audit["adx"]),
            "avg_volume_ratio": average(
                audit["volume_ratio"]
            ),
            "completed_trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pnl": round(stats["pnl"], 2),
        })

        for field in (
            "atr",
            "signal_body_atr",
            "ema_distance_atr",
            "vwap_distance_atr",
        ):
            overall_values[field].extend(preferred(field))

        overall_values["adx"].extend(audit["adx"])
        overall_values["volume_ratio"].extend(
            audit["volume_ratio"]
        )

        for filter_name, count in stats["reasons"].items():
            denominator = stats["evaluations"]

            rejection_rows.append({
                "date": day,
                "source": "audit",
                "filter": filter_name,
                "count": count,
                "denominator": denominator,
                "percentage": (
                    round(count / denominator * 100, 2)
                    if denominator
                    else None
                ),
            })

        for filter_name, count in stats[
            "quality_failures"
        ].items():
            denominator = stats["quality_rows"]

            rejection_rows.append({
                "date": day,
                "source": "entry_quality",
                "filter": filter_name,
                "count": count,
                "denominator": denominator,
                "percentage": (
                    round(count / denominator * 100, 2)
                    if denominator
                    else None
                ),
            })

        for field in (
            "evaluations",
            "candidates",
            "quality_rows",
            "trades",
            "wins",
            "losses",
            "flat",
        ):
            totals[field] += stats[field]

    overall_summary = {
        "trading_days_found": len(days),
        "date_from": min(days),
        "date_to": max(days),
        "evaluations": totals["evaluations"],
        "candidates": totals["candidates"],
        "entry_quality_rows": totals["quality_rows"],
        "completed_trades": totals["trades"],
        "wins": totals["wins"],
        "losses": totals["losses"],
        "flat_trades": totals["flat"],
        "total_pnl": round(
            sum(day["pnl"] for day in days.values()),
            2,
        ),
        "weighted_averages": {
            field: average(values)
            for field, values in sorted(
                overall_values.items()
            )
        },
        "notes": [
            "Averages are weighted using every available record.",
            "Filter percentages overlap because one candidate can fail multiple filters.",
            "Exact entry-quality values depend on retained systemd journal records.",
            "Missing journal days use audit-derived values where available.",
        ],
    }

    write_csv(
        args.output_dir / "daily_summary.csv",
        daily_rows,
    )

    write_csv(
        args.output_dir / "filter_rejections_by_day.csv",
        rejection_rows,
    )

    with (
        args.output_dir / "overall_summary.json"
    ).open("w", encoding="utf-8") as handle:
        json.dump(overall_summary, handle, indent=2)

    print()
    print("ANALYSIS COMPLETED")
    print("------------------")
    print(f"Trading days : {len(days)}")
    print(f"From         : {min(days)}")
    print(f"To           : {max(days)}")
    print(f"Evaluations  : {totals['evaluations']:,}")
    print(f"Candidates   : {totals['candidates']:,}")
    print(f"Trades       : {totals['trades']:,}")
    print(f"Total P&L    : ₹{overall_summary['total_pnl']:,.2f}")
    print()
    print("Overall weighted averages:")

    for field, value in overall_summary[
        "weighted_averages"
    ].items():
        print(f"  {field}: {value if value is not None else 'N/A'}")

    print()
    print(f"Reports: {args.output_dir.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
