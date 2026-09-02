#!/usr/bin/env python3
"""
Breakout-validation sensitivity analysis.

Uses the four real, already-computed boolean flags stored in every audit
record's breakout_validation.metrics (structure_confirmed, volume_confirmed,
volatility_confirmed, clv_confirmed -- confirmed present in real records
this session) to answer: how many evaluations would have passed breakout
validation under different pass-thresholds?

  ALL 4 required   -- the current, actual rule
  ANY 3 of 4        -- one check allowed to fail
  ANY 2 of 4        -- two checks allowed to fail
  ANY 1 of 4        -- only one check needs to pass

This does NOT simulate the full pipeline -- it only answers "would this
evaluation have ccleared the breakout-validation stage specifically."
Downstream gates (confirmation count, cost coverage, VWAP, ADX) still
apply independently and are reported separately, so you can see how many
of the newly-passing evaluations would have ACTUALLY gone on to trade
versus just cleared this one stage.

Usage:
    python3 breakout_sensitivity.py --date 2026-08-17
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path


DEFAULT_AUDIT_PATH = "runtime/live_combined_audit/entry_audit.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

CHECK_KEYS = ["structure_confirmed", "volume_confirmed", "volatility_confirmed", "clv_confirmed"]


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


def n_confirmed(record: dict) -> int | None:
    """Return how many of the 4 sub-checks passed, or None if the metrics
    aren't present (record never reached breakout validation)."""
    bv = record.get("breakout_validation") or {}
    metrics = bv.get("metrics") or {}
    if not any(k in metrics for k in CHECK_KEYS):
        return None
    return sum(1 for k in CHECK_KEYS if metrics.get(k) is True)


def other_top_level_blockers(record: dict) -> list[str]:
    """Top-level rejection reasons EXCLUDING breakout validation itself --
    these would still apply even if breakout validation were loosened."""
    reasons = record.get("reasons") or []
    return [r for r in reasons if r != "BREAKOUT_VALIDATION_FAILED"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (IST)")
    parser.add_argument("--audit-path", default=DEFAULT_AUDIT_PATH)
    args = parser.parse_args()

    date_str = args.date or datetime.now(IST).strftime("%Y-%m-%d")
    audit_path = Path(args.audit_path)

    all_rows = load_jsonl(audit_path)
    if not all_rows:
        print(f"ERROR: no records found at {audit_path.resolve()}")
        return

    today = [r for r in all_rows
             if str(r.get("logged_at", "")).startswith(date_str)
             and r.get("stage") == "CANDLE_ELIGIBILITY"]
    if not today:
        print(f"No CANDLE_ELIGIBILITY records for {date_str}.")
        return

    scored = []
    skipped_no_metrics = 0
    for r in today:
        n = n_confirmed(r)
        if n is None:
            skipped_no_metrics += 1
            continue
        scored.append((r, n))

    total = len(scored)
    section(f"BREAKOUT-VALIDATION SENSITIVITY -- {date_str}")
    print(f"Total evaluations with breakout-validation metrics: {total}")
    if skipped_no_metrics:
        print(f"(Skipped {skipped_no_metrics} records with no breakout_validation.metrics -- "
              f"never reached that stage, e.g. blocked earlier by ADX or freshness)")

    section("HOW MANY EVALUATIONS PASS UNDER EACH RULE")
    for threshold, label in [(4, "ALL 4 required (current rule)"),
                              (3, "ANY 3 of 4"),
                              (2, "ANY 2 of 4"),
                              (1, "ANY 1 of 4")]:
        passing = [r for r, n in scored if n >= threshold]
        print(f"  {label:<32} {len(passing):5d} / {total}  ({pct(len(passing), total)})")

    section("OF THE NEWLY-PASSING EVALUATIONS, HOW MANY WOULD STILL BE BLOCKED DOWNSTREAM?")
    print("(Loosening breakout validation does not bypass confirmation-count,")
    print(" cost-coverage, VWAP, or ADX checks -- those apply independently.)")
    print()
    for threshold, label in [(3, "ANY 3 of 4"), (2, "ANY 2 of 4"), (1, "ANY 1 of 4")]:
        newly_passing = [r for r, n in scored if n >= threshold and n < 4]
        still_blocked = 0
        would_clear_everything = 0
        blocker_counts = Counter()
        for r, n in [(r, n) for r, n in scored if n >= threshold and n < 4]:
            others = other_top_level_blockers(r)
            if others:
                still_blocked += 1
                for o in others:
                    blocker_counts[o] += 1
            else:
                would_clear_everything += 1
        print(f"--- {label} ---")
        print(f"  Newly passing (would not have passed under ALL-4): {len(newly_passing)}")
        print(f"  Of those, still blocked by another gate:            {still_blocked}")
        print(f"  Of those, would have cleared EVERY gate (real trade candidate): {would_clear_everything}")
        if blocker_counts:
            print(f"  What blocked the rest, most common:")
            for reason, count in blocker_counts.most_common(5):
                print(f"    {count:5d}  {reason}")
        print()

    section("SYMBOL COUNT -- HOW MANY UNIQUE SYMBOLS WOULD HAVE A REAL TRADE CANDIDATE")
    for threshold, label in [(4, "ALL 4 (current)"), (3, "ANY 3"), (2, "ANY 2"), (1, "ANY 1")]:
        symbols_with_clean_pass = set()
        for r, n in scored:
            if n >= threshold and not other_top_level_blockers(r):
                symbols_with_clean_pass.add(r.get("symbol"))
        print(f"  {label:<20} {len(symbols_with_clean_pass)} unique symbols would have had "
              f"at least one fully-clear moment")


if __name__ == "__main__":
    main()
