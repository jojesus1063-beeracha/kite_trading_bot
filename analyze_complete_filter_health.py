#!/usr/bin/env python3
"""Read-only daily analysis of live filters, indicators, validation and watchlist."""

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

import config

DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now().date().isoformat()
AUDIT = Path("runtime/live_combined_audit/entry_audit.jsonl")
VALIDATION = Path(f"validation_events/{DATE}.jsonl")
HISTORY = Path("trade_history.jsonl")

LIMITS = {
    "adx": 20.0,
    "body_atr": 1.50,
    "ema_distance_atr": 2.00,
    "vwap_distance_atr": 2.50,
    "volume_ratio": 1.50,
    "atr_expansion": 1.20,
    "clv": 0.60,
}

ALIASES = {
    "adx": {"adx", "adx14", "adx_current"},
    "rsi": {"rsi", "rsi14"},
    "ema9": {"ema9", "ema_9", "ema_entry"},
    "ema21": {"ema21", "ema_21"},
    "body_atr": {"signal_body_atr", "body_atr"},
    "ema_distance_atr": {"ema_distance_atr", "ema9_distance_atr"},
    "vwap_distance_atr": {"vwap_distance_atr"},
    "volume_ratio": {"volume_ratio"},
    "atr_expansion": {"atr_multiplier", "atr_expansion", "atr_expansion_ratio"},
    "clv": {"clv", "close_location_value"},
    "confirmation_count": {"confirmation_count"},
    "quality_score": {"entry_quality_score", "quality_score"},
    "ranking_score": {"ranking_score"},
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def metric(row, names):
    for key, value in walk(row):
        if key in names:
            result = number(value)
            if result is not None:
                return result
    return None


def record_date(row):
    payload = row.get("payload") or {}
    signal = payload.get("signal") or {}
    for value in (
        row.get("session_date"), row.get("date"), row.get("logged_at"),
        row.get("recorded_at"), row.get("timestamp"), payload.get("timestamp"),
        signal.get("timestamp"),
    ):
        if value:
            return str(value)[:10]
    return None


def load(path, date=None):
    rows, malformed = [], 0
    if not path.exists():
        return rows, malformed
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if date and record_date(row) != date:
            continue
        rows.append(row)
    return rows, malformed


def reasons(row):
    found = []
    payload = row.get("payload") or {}
    candle = row.get("candle_eligibility") or {}
    for value in (
        row.get("reasons"), payload.get("reasons"),
        payload.get("rejection_reasons"), payload.get("reason_code"),
        candle.get("reasons"), candle.get("failed_reasons"),
    ):
        if isinstance(value, list):
            found.extend(map(str, value))
        elif value:
            found.append(str(value))
    return list(dict.fromkeys(found))


def symbol(row):
    payload = row.get("payload") or {}
    signal = payload.get("signal") or {}
    return row.get("symbol") or payload.get("symbol") or signal.get("symbol") or "UNKNOWN"


def direction(row):
    payload = row.get("payload") or {}
    signal = payload.get("signal") or {}
    return (row.get("final_direction") or row.get("direction") or
            payload.get("direction") or signal.get("direction") or "UNKNOWN")


def describe(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return "unavailable"
    pick = lambda fraction: values[round((len(values) - 1) * fraction)]
    return (f"n={len(values)} min={values[0]:.4f} p25={pick(.25):.4f} "
            f"avg={mean(values):.4f} p50={pick(.5):.4f} "
            f"p75={pick(.75):.4f} max={values[-1]:.4f}")


def heading(text):
    print(f"\n{text}\n{'=' * len(text)}")


def audit_report(rows):
    heading("LIVE AUDIT")
    decisions = Counter(row.get("decision") or row.get("event") or "UNKNOWN" for row in rows)
    stages = Counter(row.get("stage") or "UNKNOWN" for row in rows)
    overlapping, sole = Counter(), Counter()
    grouped = defaultdict(list)
    for row in rows:
        rejected_by = reasons(row)
        for item in rejected_by:
            overlapping[item] += 1
            grouped[item].append(row)
        if len(rejected_by) == 1:
            sole[rejected_by[0]] += 1
    print("Records:", len(rows))
    print("Decisions:", dict(decisions))
    print("Stages:", dict(stages))
    print("Unique symbols:", len({symbol(row) for row in rows}))
    print("Directions:", dict(Counter(direction(row) for row in rows)))

    heading("ALL OVERLAPPING REJECTIONS")
    for item, count in overlapping.most_common():
        pct = count / len(rows) * 100 if rows else 0
        print(f"{item:<58} {count:>6} {pct:>7.2f}%")

    heading("TRUE SOLE BLOCKERS")
    for item, count in sole.most_common():
        pct = count / len(rows) * 100 if rows else 0
        print(f"{item:<58} {count:>6} {pct:>7.2f}%")

    heading("INDICATORS: SELECTED VS REJECTED")
    for label, aliases in ALIASES.items():
        selected, rejected = [], []
        for row in rows:
            value = metric(row, aliases)
            if value is None:
                continue
            if row.get("decision") == "SIGNAL_SELECTED":
                selected.append(value)
            else:
                rejected.append(value)
        print(label)
        print("  selected:", describe(selected))
        print("  rejected:", describe(rejected))

    heading("VALUES BY REJECTION REASON")
    for item, count in overlapping.most_common():
        print(f"\n{item} | {count}")
        for label, aliases in ALIASES.items():
            values = [metric(row, aliases) for row in grouped[item]]
            values = [value for value in values if value is not None]
            if values:
                print(f"  {label:<24} {describe(values)}")


def validation_report(rows):
    heading("VALIDATION AND ENTRY QUALITY")
    print("Events:", dict(Counter(row.get("event_type", "UNKNOWN") for row in rows)))
    print("Reasons:", dict(Counter(
        (row.get("payload") or {}).get("reason_code")
        for row in rows if (row.get("payload") or {}).get("reason_code")
    )))
    quality = []
    for row in rows:
        payload = row.get("payload") or {}
        signal = payload.get("signal") or payload
        detail = payload.get("entry_quality_detail") or signal.get("entry_quality_detail") or {}
        if not detail:
            continue
        quality.append({
            "symbol": payload.get("symbol") or signal.get("symbol") or "UNKNOWN",
            "direction": payload.get("direction") or signal.get("direction") or "UNKNOWN",
            "body_atr": number(detail.get("signal_body_atr")),
            "ema_distance_atr": number(detail.get("ema_distance_atr")),
            "vwap_distance_atr": number(detail.get("vwap_distance_atr")),
            "atr": number(detail.get("atr")),
            "reason": payload.get("reason_code"),
        })
    for key in ("atr", "body_atr", "ema_distance_atr", "vwap_distance_atr"):
        print(f"{key}: {describe([row[key] for row in quality])}")

    heading("ENTRY-QUALITY NEAR MISSES")
    misses = []
    for row in quality:
        failed = []
        for key in ("body_atr", "ema_distance_atr", "vwap_distance_atr"):
            value, limit = row[key], LIMITS[key]
            if value is not None and value > limit:
                excess = (value / limit - 1) * 100
                failed.append((excess, f"{key}={value:.4f}>{limit:.2f}"))
        if failed:
            misses.append((len(failed), min(x[0] for x in failed), row, failed))
    for _, closest, row, failed in sorted(misses):
        print(f"{row['symbol']:<14} {row['direction']:<5} failures={len(failed)} "
              f"closest_over={closest:.2f}% | " + "; ".join(x[1] for x in failed))


def watchlist_report():
    heading("WATCHLIST SELECTOR")
    files = sorted(set(
        list(Path("runtime").rglob("*watchlist*.jsonl")) +
        list(Path("runtime").rglob("selection_audit.jsonl"))
    )) if Path("runtime").exists() else []
    if not files:
        print("No watchlist selector audit JSONL found.")
    for path in files:
        rows, malformed = load(path)
        events, rejection_reasons = Counter(), Counter()
        for row in rows:
            payload = row.get("payload") or {}
            events[row.get("event_type") or row.get("event") or "UNKNOWN"] += 1
            value = (row.get("reason_code") or row.get("reason") or
                     payload.get("reason_code") or payload.get("reason"))
            if value:
                rejection_reasons[str(value)] += 1
        print(f"\nFile: {path}\nRecords: {len(rows)} | Malformed: {malformed}")
        print("Events:", dict(events))
        print("Rejection reasons:", dict(rejection_reasons))

    heading("WATCHLIST CONFIG VALUES")
    found = False
    for name in sorted(dir(config)):
        upper = name.upper()
        if not any(word in upper for word in ("WATCHLIST", "EMA200", "UNIVERSE", "SECTOR", "SYMBOL")):
            continue
        if any(word in upper for word in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            continue
        value = getattr(config, name)
        if isinstance(value, (str, int, float, bool, list, tuple, set)):
            print(f"{name} = {repr(value)[:600]}")
            found = True
    if not found:
        print("No matching scalar config values found.")


def trade_report():
    rows, malformed = load(HISTORY, DATE)
    heading("ACTUAL TRADES")
    total = wins = gross = costs = 0.0
    for row in rows:
        net = number(row.get("net_pnl"))
        if net is None:
            net = number(row.get("pnl")) or 0.0
        trade_cost = number(row.get("costs") or row.get("charges")) or 0.0
        trade_gross = number(row.get("gross_pnl"))
        if trade_gross is None:
            trade_gross = net + trade_cost
        total += net
        gross += trade_gross
        costs += trade_cost
        wins += net > 0
        print(row.get("time"), row.get("symbol"), row.get("direction"),
              f"qty={row.get('qty')} result={row.get('result')} net=₹{net:.2f}")
    print(f"Trades: {len(rows)} | Wins: {int(wins)} | Losses: {len(rows)-int(wins)}")
    print(f"Gross: ₹{gross:.2f} | Costs: ₹{costs:.2f} | Net P&L: ₹{total:.2f}")
    print("Malformed:", malformed)


def config_report():
    heading("CONFIRMED LIVE LIMITS")
    for key, value in LIMITS.items():
        print(f"{key}: {value}")
    for key in ("CAPITAL", "RISK_PER_TRADE_PCT", "MAX_DAILY_LOSS_PCT",
                "MAX_TRADES_PER_DAY", "MAX_OPEN_POSITIONS"):
        if hasattr(config, key):
            print(f"config.{key}: {getattr(config, key)!r}")


def main():
    print("COMPLETE FILTER, INDICATOR AND WATCHLIST REPORT")
    print("Date:", DATE)
    config_report()
    audit, bad_audit = load(AUDIT, DATE)
    print("\nMalformed live-audit rows:", bad_audit)
    audit_report(audit)
    validation, bad_validation = load(VALIDATION, DATE)
    print("\nMalformed validation rows:", bad_validation)
    validation_report(validation)
    watchlist_report()
    trade_report()
    heading("INTERPRETATION")
    print("Overlapping percentages can exceed 100% because one evaluation may fail several filters.")
    print("Sole blockers are the best candidates for controlled counterfactual replay.")
    print("Near miss does not mean profitable; price replay is required before changing live limits.")
    print("RPT changes quantity only; it does not create additional signals.")


if __name__ == "__main__":
    main()
