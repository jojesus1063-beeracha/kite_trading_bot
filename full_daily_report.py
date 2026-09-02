#!/usr/bin/env python3
"""
Full daily trading report -- everything in one run.

Combines:
  1. Actual trades taken today, with real P&L
  2. Total entry evaluations and unique symbols scanned
  3. Top-level rejection reason breakdown (% of all evaluations)
  4. Breakout-validation sub-reason breakdown
  5. Per-gate strictness -- confirmation count, cost coverage, VWAP,
     ADX -- each with how close the BLOCKED evaluations actually were
     to passing, not just pass/fail counts
  6. Near-miss symbol ranking -- who came closest to a clean trade today
  7. Plain-language summary

This consolidates the logic already verified in daily_entry_insight.py
and filter_strictness.py into a single report.

Usage:
    python3 full_daily_report.py                    # today, IST
    python3 full_daily_report.py --date 2026-08-18
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


DEFAULT_AUDIT_PATH = "runtime/live_combined_audit/entry_audit.jsonl"
DEFAULT_TRADE_LOG_PATH = "trade_history.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def pct(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n/total*100:5.1f}%"


def report_trades(trades_today: list[dict]) -> None:
    section("TRADES TAKEN TODAY")
    if not trades_today:
        print("No trades taken today.")
        return
    net = sum(t.get("pnl", 0) or 0 for t in trades_today)
    wins = sum(1 for t in trades_today if (t.get("pnl", 0) or 0) > 0)
    print(f"Trades: {len(trades_today)}  |  Wins: {wins}  |  Losses: {len(trades_today) - wins}  |  Net P&L: {net:+.2f}")
    print()
    for t in sorted(trades_today, key=lambda x: x.get("time") or x.get("entry_time") or ""):
        ts = t.get("time") or t.get("entry_time")
        symbol = t.get("symbol")
        direction = t.get("direction")
        pnl = t.get("pnl")
        reason = t.get("exit_reason") or t.get("result")
        print(f"  {ts} | {symbol:<14} | {direction:<4} | pnl={pnl:+.2f} | {reason}")


def report_overview_and_reasons(today: list[dict]) -> Counter:
    total = len(today)
    symbols = {r.get("symbol") for r in today}
    section("ENTRY EVALUATION OVERVIEW")
    print(f"Total evaluations: {total}")
    print(f"Unique symbols evaluated: {len(symbols)}")

    section("TOP-LEVEL REJECTION REASONS")
    counts = Counter()
    for r in today:
        for reason in (r.get("reasons") or []):
            counts[reason] += 1
    for reason, count in counts.most_common():
        print(f"  {count:5d}  ({pct(count, total)})  {reason}")
    return counts


def report_breakout_sub_reasons(today: list[dict]) -> None:
    section("BREAKOUT VALIDATION SUB-REASONS")
    bv_records = [r for r in today if r.get("breakout_validation")]
    if not bv_records:
        print("No breakout_validation records found.")
        return
    counts = Counter()
    for r in bv_records:
        bv = r.get("breakout_validation") or {}
        for reason in (bv.get("reasons") or []):
            counts[reason] += 1
    print(f"Records with breakout_validation present: {len(bv_records)}")
    for reason, count in counts.most_common():
        print(f"  {count:5d}  ({pct(count, len(bv_records))})  {reason}")


def report_confirmation_count(today: list[dict]) -> None:
    section("GATE 1 -- CONFIRMATION COUNT (needs 2 of 3)")
    counts = {}
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        cc = ce.get("confirmation_count")
        if cc is None:
            continue
        counts[cc] = counts.get(cc, 0) + 1
    if not counts:
        print("confirmation_count not found for this date.")
        return
    total = sum(counts.values())
    for n in sorted(counts):
        marker = "  <- BLOCKS" if n < 2 else "  <- passes"
        print(f"  confirmation_count = {n}:  {counts[n]:5d}  ({pct(counts[n], total)}){marker}")
    near_miss = counts.get(1, 0)
    blocked = counts.get(0, 0) + counts.get(1, 0)
    print(f"Near-misses (1 of 3): {near_miss} ({pct(near_miss, total)})  |  Total blocked: {blocked} ({pct(blocked, total)})")


def report_cost_gate(today: list[dict]) -> None:
    section("GATE 2 -- COST COVERAGE")
    ratios = []
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        cam = ce.get("cost_aware_movement") or {}
        expected, required = cam.get("expected_gross_pnl"), cam.get("required_gross_pnl")
        if expected is None or required is None or required == 0:
            continue
        ratios.append(expected / required)
    if not ratios:
        print("cost_aware_movement data not found for this date.")
        return
    total = len(ratios)
    passed = sum(1 for r in ratios if r >= 1.0)
    blocked = total - passed
    print(f"Passed: {passed} ({pct(passed, total)})  |  Blocked: {blocked} ({pct(blocked, total)})")
    if blocked:
        close_misses = sum(1 for r in ratios if 0.5 <= r < 1.0)
        print(f"Of blocked, close (50-99% of required): {close_misses} ({pct(close_misses, blocked)} of blocked)")


def report_vwap_gate(today: list[dict]) -> None:
    section("GATE 3 -- VWAP ALIGNMENT")
    total, passed_count, distances = 0, 0, []
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        close, vwap = ce.get("entry_close"), ce.get("vwap")
        direction = ce.get("direction") or r.get("final_direction")
        if close is None or vwap is None or vwap == 0 or direction not in ("BUY", "SELL"):
            continue
        total += 1
        dist_pct = (close - vwap) / vwap * 100
        passed = dist_pct > 0 if direction == "BUY" else dist_pct < 0
        if passed:
            passed_count += 1
        else:
            distances.append(abs(dist_pct))
    if total == 0:
        print("VWAP data not found for this date.")
        return
    blocked = total - passed_count
    print(f"Passed: {passed_count} ({pct(passed_count, total)})  |  Blocked: {blocked} ({pct(blocked, total)})")
    if distances:
        distances.sort()
        print(f"Median distance on wrong side, when blocked: {distances[len(distances)//2]:.3f}%")


def report_adx_gate(today: list[dict]) -> None:
    section("GATE 4 -- ADX STRENGTH (BUY >= 25, SELL >= 20)")
    total, passed_count, margins = 0, 0, []
    for r in today:
        adx, direction = r.get("adx"), r.get("final_direction")
        if adx is None or direction not in ("BUY", "SELL"):
            continue
        min_adx = 25.0 if direction == "BUY" else 20.0
        total += 1
        margin = adx - min_adx
        if margin >= 0:
            passed_count += 1
        else:
            margins.append(abs(margin))
    if total == 0:
        print("ADX/direction data not found for this date.")
        return
    blocked = total - passed_count
    print(f"Passed: {passed_count} ({pct(passed_count, total)})  |  Blocked: {blocked} ({pct(blocked, total)})")
    if margins:
        margins.sort()
        print(f"Median shortfall when blocked: {margins[len(margins)//2]:.1f} ADX points")


def report_near_miss(today: list[dict], top_n: int = 10) -> list[dict]:
    section(f"SYMBOLS CLOSEST TO A CLEAN TRADE TODAY (top {top_n})")
    by_symbol = defaultdict(list)
    for r in today:
        by_symbol[r.get("symbol")].append(r)

    stats = []
    for symbol, records in by_symbol.items():
        best_near_miss = 99
        best_record = None
        for r in records:
            bv = r.get("breakout_validation") or {}
            n_failed = len(bv.get("reasons") or [])
            if n_failed < best_near_miss:
                best_near_miss = n_failed
                best_record = r
        stats.append({"symbol": symbol, "best_near_miss": best_near_miss, "best_record": best_record})

    stats.sort(key=lambda s: s["best_near_miss"])
    for s in stats[:top_n]:
        r = s["best_record"]
        other = [x for x in (r.get("reasons") or []) if x != "BREAKOUT_VALIDATION_FAILED"] if r else []
        print(f"  {s['symbol']:<14} breakout_sub_fails={s['best_near_miss']}  other_blockers={other}")
    return stats


def report_summary(total: int, trades_today: list[dict], top_reasons: Counter, stats: list[dict]) -> None:
    section("PLAIN-LANGUAGE SUMMARY")
    if trades_today:
        net = sum(t.get("pnl", 0) or 0 for t in trades_today)
        print(f"{len(trades_today)} trade(s) taken today, net P&L {net:+.2f}.")
    else:
        print("No trades taken today.")
        zero_miss = sum(1 for s in stats if s["best_near_miss"] == 0)
        if zero_miss:
            print(f"{zero_miss} symbol(s) cleared breakout validation cleanly at least once, "
                  f"but were still blocked by another gate.")
    if top_reasons:
        reason, count = top_reasons.most_common(1)[0]
        print(f"Most common blocking reason: {reason} ({count} of {total}, {pct(count, total)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (IST)")
    parser.add_argument("--audit-path", default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--trade-log-path", default=DEFAULT_TRADE_LOG_PATH)
    args = parser.parse_args()

    date_str = args.date or datetime.now(IST).strftime("%Y-%m-%d")
    audit_path, trade_path = Path(args.audit_path), Path(args.trade_log_path)

    all_audit = load_jsonl(audit_path)
    if not all_audit:
        print(f"ERROR: no records found at {audit_path.resolve()}")
        return

    today = [r for r in all_audit
             if str(r.get("logged_at", "")).startswith(date_str)
             and r.get("stage") == "CANDLE_ELIGIBILITY"]

    all_trades = load_jsonl(trade_path)
    trades_today = [t for t in all_trades if (t.get("date") or (t.get("entry_time") or "")[:10]) == date_str]

    print("#" * 78)
    print(f"FULL DAILY REPORT -- {date_str}")
    print("#" * 78)

    report_trades(trades_today)

    if not today:
        print(f"\nNo entry-evaluation records found for {date_str}. "
              f"(Found {len(all_audit)} total records in the audit file.)")
        return

    top_reasons = report_overview_and_reasons(today)
    report_breakout_sub_reasons(today)
    report_confirmation_count(today)
    report_cost_gate(today)
    report_vwap_gate(today)
    report_adx_gate(today)
    stats = report_near_miss(today)
    report_summary(len(today), trades_today, top_reasons, stats)


if __name__ == "__main__":
    main()
