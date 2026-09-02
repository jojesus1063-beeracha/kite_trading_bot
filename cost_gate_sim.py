#!/usr/bin/env python3
"""
Cost-gate multiple simulation.

Answers: if the required cost-coverage multiple were lowered from 2.0x to
some other value, how many evaluations that are blocked ONLY by the cost
gate (already clearing breakout validation, confirmation count, VWAP, and
ADX) would newly pass?

This isolates the real marginal effect. It does NOT simply count "how many
more evaluations clear the cost check in isolation" -- that would overstate
the impact, since many cost-failing evaluations are also failing other
gates and wouldn't become trades regardless.

IMPORTANT CAVEAT printed in the output: this counts EVALUATIONS (roughly
every ~25-50 seconds per symbol), not TRADES. The same symbol can show as
a newly-passing evaluation across several consecutive cycles while a
position is open elsewhere (max_open_positions=1), or during a post-loss
cooldown. The real trade-count increase will be smaller than the number
reported here -- treat this as an upper bound, not a forecast.

Usage:
    python3 cost_gate_sim.py --date 2026-08-17 --new-multiple 1.7
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


DEFAULT_AUDIT_PATH = "runtime/live_combined_audit/entry_audit.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))
CURRENT_MULTIPLE = 2.0

NON_COST_REASONS_THAT_STILL_BLOCK = {
    "BREAKOUT_VALIDATION_FAILED",
    "INSUFFICIENT_ENTRY_CONFIRMATIONS",
    "VWAP_DIRECTION_NOT_ACCEPTED_OR_UNAVAILABLE",
    "ADX_STRENGTH_BELOW_MINIMUM_OR_UNAVAILABLE",
    "CANDLE_NOT_COMPLETED_OR_FRESH",
    "CONFLICTING_TIER1_PATTERNS",
}


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (IST)")
    parser.add_argument("--audit-path", default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--new-multiple", type=float, required=True,
                         help="The proposed new cost-coverage multiple (current is 2.0)")
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

    section(f"COST-GATE SIMULATION -- {date_str}")
    print(f"Current required multiple: {CURRENT_MULTIPLE}x")
    print(f"Proposed new multiple:     {args.new_multiple}x")

    total = len(today)
    already_passing = 0
    cost_only_blocker = []
    other_reasons_present = 0
    no_cost_data = 0

    for r in today:
        reasons = set(r.get("reasons") or [])
        detail = (r.get("candle_eligibility") or {}).get("detail") or {}
        cam = detail.get("cost_aware_movement") or {}
        expected = cam.get("expected_gross_pnl")
        required = cam.get("required_gross_pnl")

        if "EXPECTED_MOVE_DOES_NOT_COVER_COSTS" not in reasons:
            already_passing += 1
            continue

        other_blockers = reasons & NON_COST_REASONS_THAT_STILL_BLOCK
        if other_blockers:
            other_reasons_present += 1
            continue

        if expected is None or required is None or required == 0:
            no_cost_data += 1
            continue

        ratio_to_current_required = expected / required
        cost_only_blocker.append((r.get("symbol"), r.get("logged_at"), expected, required, ratio_to_current_required))

    section("BREAKDOWN")
    print(f"Total evaluations: {total}")
    print(f"Already passing the cost gate (no change needed): {already_passing} ({pct(already_passing, total)})")
    print(f"Blocked by cost gate AND at least one other gate (would NOT become a trade regardless): {other_reasons_present} ({pct(other_reasons_present, total)})")
    print(f"Blocked by cost gate ONLY -- these are the real candidates: {len(cost_only_blocker)} ({pct(len(cost_only_blocker), total)})")
    if no_cost_data:
        print(f"(Skipped {no_cost_data} with missing cost data)")

    scale = args.new_multiple / CURRENT_MULTIPLE
    newly_passing = []
    for symbol, ts, expected, required, ratio in cost_only_blocker:
        new_required = required * scale
        if expected >= new_required:
            newly_passing.append((symbol, ts, expected, required, new_required))

    section(f"RESULT AT {args.new_multiple}x")
    n_candidates = len(cost_only_blocker)
    n_newly_passing = len(newly_passing)
    print(f"Of the {n_candidates} evaluations blocked ONLY by cost:")
    print(f"  Would newly pass at {args.new_multiple}x: {n_newly_passing} ({pct(n_newly_passing, n_candidates)} of that group)")
    print(f"  As a share of ALL {total} evaluations today: {pct(n_newly_passing, total)}")

    unique_symbols = {s for s, ts, e, r, nr in newly_passing}
    print(f"  Unique symbols affected: {len(unique_symbols)}")
    if unique_symbols:
        print(f"    {', '.join(sorted(unique_symbols))}")

    print()
    print("*** IMPORTANT: these are EVALUATIONS, not trades. The same symbol can")
    print("*** appear multiple times across the day. Max 1 open position and")
    print("*** symbol cooldowns mean the real trade-count increase will be")
    print("*** SMALLER than this number. Treat this as an upper bound.")

    if newly_passing:
        section("DETAIL -- WHAT WOULD NEWLY PASS")
        for symbol, ts, expected, required, new_required in newly_passing[:20]:
            print(f"  {symbol:<14} {ts}  expected={expected:.2f}  "
                  f"old_required={required:.2f}  new_required={new_required:.2f}")


if __name__ == "__main__":
    main()
