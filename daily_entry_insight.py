#!/usr/bin/env python3
"""
Daily entry-signal insight report.

Reads runtime/live_combined_audit/entry_audit.jsonl (the real, live audit
trail written by paper_contrarian_launcher.py's install_two_indicator_patch()
in live_combined mode) and produces a complete breakdown of:

  1. Overview -- total evaluations, unique symbols, trades taken
  2. Top-level rejection reasons, with % of all evaluations
  3. Breakout-validation sub-reasons, with % of records that reached that stage
  4. Redundancy check -- how many gates fail SIMULTANEOUSLY per evaluation
     (tells you whether one gate is the bottleneck, or setups are genuinely
     weak on multiple independent measures at once)
  5. Per-symbol "near miss" ranking -- which symbols came closest to a
     valid breakout at their best moment today, with their real metrics
  6. For the closest near-misses, the EXACT reason they were still blocked
     (since passing breakout validation does not guarantee an entry --
     other gates like confirmation count, VWAP, or cost coverage can still
     block it)
  7. Price-action score distribution (positive/zero/negative split)
  8. A plain-language summary at the end

Every metric here was independently built and verified against real audit
records during tonight's session -- this consolidates that work into one
reusable tool rather than repeated one-off queries.

Usage:
    python3 daily_entry_insight.py                  # today, IST
    python3 daily_entry_insight.py --date 2026-08-17 # specific date
    python3 daily_entry_insight.py --audit-path /custom/path/entry_audit.jsonl
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


def filter_by_date(rows: list[dict], date_str: str, field: str = "logged_at") -> list[dict]:
    return [r for r in rows if str(r.get(field, "")).startswith(date_str)]


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def pct(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n/total*100:5.1f}%"


def overview(today: list[dict], trades_today: list[dict], date_str: str) -> int:
    section(f"OVERVIEW -- {date_str}")
    total = len(today)
    symbols = {r.get("symbol") for r in today}
    print(f"Total entry evaluations: {total}")
    print(f"Unique symbols evaluated: {len(symbols)}")
    print(f"Trades actually taken today: {len(trades_today)}")
    if trades_today:
        net = sum(t.get("pnl", 0) or 0 for t in trades_today)
        print(f"Net P&L today: {net:+.2f}")
    return total


def top_level_reasons(today: list[dict], total: int) -> None:
    section("TOP-LEVEL REJECTION REASONS")
    counts = Counter()
    for r in today:
        for reason in (r.get("reasons") or []):
            counts[reason] += 1
    if not counts:
        print("No rejection reasons recorded (or all evaluations were accepted).")
        return
    for reason, count in counts.most_common():
        print(f"  {count:5d}  ({pct(count, total)})  {reason}")


def breakout_sub_reasons(today: list[dict]) -> None:
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


def redundancy_check(today: list[dict]) -> None:
    section("HOW MANY TOP-LEVEL REASONS FIRE PER EVALUATION")
    print("(High counts at 2+ reasons mean setups are genuinely weak on")
    print(" multiple independent measures at once -- not one narrow gate")
    print(" blocking otherwise-good trades.)")
    print()
    dist = Counter(len(r.get("reasons") or []) for r in today)
    for n_reasons in sorted(dist):
        count = dist[n_reasons]
        print(f"  {count:5d} evaluations failed exactly {n_reasons} top-level reason(s)")


def near_miss_ranking(today: list[dict], top_n: int = 15) -> list[dict]:
    section(f"SYMBOLS CLOSEST TO A VALID BREAKOUT TODAY (top {top_n})")
    by_symbol = defaultdict(list)
    for r in today:
        by_symbol[r.get("symbol")].append(r)

    stats = []
    for symbol, records in by_symbol.items():
        vol_ratios, atr_mults, pa_scores, adx_vals = [], [], [], []
        best_near_miss = 99
        best_record = None
        for r in records:
            bv = (r.get("breakout_validation") or {})
            metrics = bv.get("metrics") or {}
            n_failed = len(bv.get("reasons") or [])
            if n_failed < best_near_miss:
                best_near_miss = n_failed
                best_record = r
            if metrics.get("volume_ratio") is not None:
                vol_ratios.append(metrics["volume_ratio"])
            if metrics.get("atr_multiplier") is not None:
                atr_mults.append(metrics["atr_multiplier"])
            pa = r.get("price_action_confirmation") or {}
            if pa.get("score") is not None:
                pa_scores.append(pa["score"])
            if r.get("adx") is not None:
                adx_vals.append(r["adx"])

        stats.append({
            "symbol": symbol,
            "n_evaluations": len(records),
            "best_near_miss": best_near_miss,
            "best_record": best_record,
            "max_vol_ratio": max(vol_ratios) if vol_ratios else None,
            "max_atr_mult": max(atr_mults) if atr_mults else None,
            "max_pa_score": max(pa_scores) if pa_scores else None,
            "avg_adx": (sum(adx_vals) / len(adx_vals)) if adx_vals else None,
        })

    stats.sort(key=lambda s: (s["best_near_miss"], -(s["max_vol_ratio"] or 0)))
    for s in stats[:top_n]:
        adx_str = f"{s['avg_adx']:.1f}" if s["avg_adx"] is not None else "n/a"
        vol_str = f"{s['max_vol_ratio']:.2f}" if s["max_vol_ratio"] is not None else "n/a"
        atr_str = f"{s['max_atr_mult']:.2f}" if s["max_atr_mult"] is not None else "n/a"
        pa_str = str(s["max_pa_score"]) if s["max_pa_score"] is not None else "n/a"
        print(f"  {s['symbol']:<14} near_miss={s['best_near_miss']}  "
              f"max_vol_ratio={vol_str}  max_atr_mult={atr_str}  "
              f"max_pa_score={pa_str}  avg_adx={adx_str}")
    return stats


def zero_miss_deep_dive(stats: list[dict], limit: int = 8) -> None:
    """For symbols that passed ALL breakout sub-checks at least once,
    show exactly what STILL blocked them -- since passing breakout
    validation does not guarantee an entry."""
    zero_miss = [s for s in stats if s["best_near_miss"] == 0][:limit]
    if not zero_miss:
        return
    section("WHY DID SYMBOLS THAT PASSED BREAKOUT VALIDATION STILL NOT TRADE?")
    print("(These symbols cleared structure/volume/volatility/CLV at their")
    print(" best moment today. This shows exactly what blocked them anyway.)")
    print()
    for s in zero_miss:
        r = s["best_record"]
        if r is None:
            continue
        print(f"--- {s['symbol']} --- (at {r.get('logged_at')})")
        print(f"  Direction: {r.get('final_direction')}  |  ADX: {r.get('adx')}")
        print(f"  Blocked by: {r.get('reasons')}")
        print()


def price_action_distribution(today: list[dict]) -> None:
    section("PRICE ACTION SCORE DISTRIBUTION")
    scores = []
    for r in today:
        pa = r.get("price_action_confirmation") or {}
        if pa.get("score") is not None:
            scores.append(pa["score"])
    if not scores:
        print("No price_action_confirmation scores found.")
        return
    positive = sum(1 for s in scores if s > 0)
    zero = sum(1 for s in scores if s == 0)
    negative = sum(1 for s in scores if s < 0)
    n = len(scores)
    print(f"Total scored: {n}")
    print(f"  Positive: {positive} ({pct(positive, n)})")
    print(f"  Zero:     {zero} ({pct(zero, n)})")
    print(f"  Negative: {negative} ({pct(negative, n)})")
    print(f"  Min: {min(scores)}  Max: {max(scores)}  Avg: {sum(scores)/n:.2f}")


def plain_summary(total: int, trades_today: list[dict], top_reason_counts: Counter,
                   zero_miss_count: int) -> None:
    section("PLAIN-LANGUAGE SUMMARY")
    if trades_today:
        print(f"{len(trades_today)} trade(s) were taken today.")
    else:
        print("No trades were taken today.")
        if zero_miss_count > 0:
            print(f"However, {zero_miss_count} symbol(s) had at least one moment where")
            print("every quality check (structure, volume, volatility, close-position)")
            print("passed -- those were blocked by OTHER rules (cost coverage, ADX")
            print("strength, confirmation count, or VWAP alignment), not by weak setups.")
            print("See the deep-dive section above for exactly which rule blocked each one.")
        else:
            print("No symbol passed all breakout quality checks even once today --")
            print("consistent with a genuinely low-conviction, range-bound session.")
    print()
    if top_reason_counts:
        top_reason, top_count = top_reason_counts.most_common(1)[0]
        print(f"The single most common blocking reason today was: {top_reason}")
        print(f"({top_count} of {total} evaluations, {pct(top_count, total)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None,
                         help="Date to analyze, YYYY-MM-DD. Defaults to today (IST).")
    parser.add_argument("--audit-path", default=DEFAULT_AUDIT_PATH,
                         help=f"Path to entry_audit.jsonl (default: {DEFAULT_AUDIT_PATH})")
    parser.add_argument("--trade-log-path", default=DEFAULT_TRADE_LOG_PATH,
                         help=f"Path to trade_history.jsonl (default: {DEFAULT_TRADE_LOG_PATH})")
    parser.add_argument("--top-n", type=int, default=15,
                         help="How many near-miss symbols to show (default 15)")
    args = parser.parse_args()

    date_str = args.date or datetime.now(IST).strftime("%Y-%m-%d")

    audit_path = Path(args.audit_path)
    trade_path = Path(args.trade_log_path)

    all_audit = load_jsonl(audit_path)
    if not all_audit:
        print(f"ERROR: no records found at {audit_path.resolve()}")
        print("Check the path, or pass --audit-path explicitly.")
        return

    today = [r for r in filter_by_date(all_audit, date_str) if r.get("stage") == "CANDLE_ELIGIBILITY"]
    if not today:
        print(f"No CANDLE_ELIGIBILITY records found for {date_str}.")
        print(f"(Found {len(all_audit)} total records in the audit file -- "
              f"check the date or whether the bot ran that day.)")
        return

    all_trades = load_jsonl(trade_path)
    trades_today = [t for t in all_trades if (t.get("date") or (t.get("entry_time") or "")[:10]) == date_str]

    total = overview(today, trades_today, date_str)
    top_level_reasons(today, total)
    breakout_sub_reasons(today)
    redundancy_check(today)
    stats = near_miss_ranking(today, top_n=args.top_n)
    zero_miss_deep_dive(stats)
    price_action_distribution(today)

    top_reason_counts = Counter()
    for r in today:
        for reason in (r.get("reasons") or []):
            top_reason_counts[reason] += 1
    zero_miss_count = sum(1 for s in stats if s["best_near_miss"] == 0)
    plain_summary(total, trades_today, top_reason_counts, zero_miss_count)


if __name__ == "__main__":
    main()
