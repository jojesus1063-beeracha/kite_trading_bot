#!/usr/bin/env python3
"""
Filter strictness analysis -- for each of the 4 major downstream gates,
shows not just how often it blocks, but how CLOSE the blocked evaluations
actually were to passing. A gate that blocks 80% of the time but everything
is a near-miss is very different from one where everything is far off.

Gates covered:
  1. INSUFFICIENT_ENTRY_CONFIRMATIONS  -- needs 2 of 3 confirmations
  2. EXPECTED_MOVE_DOES_NOT_COVER_COSTS -- expected P&L vs 2x cost hurdle
  3. VWAP_DIRECTION_NOT_ACCEPTED         -- price vs VWAP for direction
  4. ADX_STRENGTH_BELOW_MINIMUM          -- ADX vs direction-specific minimum

Usage:
    python3 filter_strictness.py --date 2026-08-17
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


DEFAULT_AUDIT_PATH = "runtime/live_combined_audit/entry_audit.jsonl"
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


def confirmation_count_analysis(today: list[dict]) -> None:
    section("1. CONFIRMATION COUNT  (needs 2 of 3: pattern / volume / price-action)")
    counts = {}
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        cc = ce.get("confirmation_count")
        if cc is None:
            continue
        counts[cc] = counts.get(cc, 0) + 1
    if not counts:
        print("confirmation_count field not found in candle_eligibility.detail for this date.")
        return
    total = sum(counts.values())
    print(f"Total evaluations with a confirmation_count value: {total}")
    print()
    for n in sorted(counts):
        marker = "  <- BLOCKS (needs >= 2)" if n < 2 else "  <- passes this gate"
        print(f"  confirmation_count = {n}:  {counts[n]:5d}  ({pct(counts[n], total)}){marker}")
    zero_or_one = counts.get(0, 0) + counts.get(1, 0)
    one_only = counts.get(1, 0)
    print()
    print(f"Near-misses (exactly 1 of 3, one signal short of passing): {one_only} ({pct(one_only, total)})")
    print(f"Total blocked by this gate: {zero_or_one} ({pct(zero_or_one, total)})")


def cost_gate_analysis(today: list[dict]) -> None:
    section("2. COST-COVERAGE GATE  (expected P&L must be >= 2x estimated cost)")
    ratios = []
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        cam = ce.get("cost_aware_movement") or {}
        expected = cam.get("expected_gross_pnl")
        required = cam.get("required_gross_pnl")
        if expected is None or required is None or required == 0:
            continue
        ratios.append(expected / required)  # >= 1.0 means it passed
    if not ratios:
        print("cost_aware_movement fields not found for this date.")
        return
    total = len(ratios)
    passed = sum(1 for r in ratios if r >= 1.0)
    print(f"Total evaluations with cost-gate data: {total}")
    print(f"Passed (expected >= 2x cost): {passed} ({pct(passed, total)})")
    print(f"Blocked: {total - passed} ({pct(total - passed, total)})")
    print()
    close_misses = sum(1 for r in ratios if 0.5 <= r < 1.0)
    far_misses = sum(1 for r in ratios if r < 0.5)
    blocked = total - passed
    print(f"Of the blocked evaluations:")
    print(f"  Close (expected P&L was 50-99% of the required amount): {close_misses} ({pct(close_misses, blocked)} of blocked)")
    print(f"  Far short (expected P&L was under 50% of required):     {far_misses} ({pct(far_misses, blocked)} of blocked)")
    sorted_ratios = sorted(ratios)
    if sorted_ratios:
        median = sorted_ratios[len(sorted_ratios) // 2]
        print(f"Median ratio (expected / required), all evaluations: {median:.2f}")


def vwap_gate_analysis(today: list[dict]) -> None:
    section("3. VWAP ALIGNMENT  (BUY needs price above VWAP, SELL needs price below)")
    distances_pct = []
    passed_count = 0
    total = 0
    for r in today:
        ce = (r.get("candle_eligibility") or {}).get("detail") or {}
        close = ce.get("entry_close")
        vwap = ce.get("vwap")
        direction = ce.get("direction") or r.get("final_direction")
        if close is None or vwap is None or vwap == 0 or direction not in ("BUY", "SELL"):
            continue
        total += 1
        dist_pct = (close - vwap) / vwap * 100
        if direction == "BUY":
            passed = dist_pct > 0
        else:
            passed = dist_pct < 0
            dist_pct = -dist_pct
        if passed:
            passed_count += 1
        else:
            distances_pct.append(abs(dist_pct))
    if total == 0:
        print("VWAP/close/direction fields not found for this date.")
        return
    print(f"Total evaluations with VWAP data: {total}")
    print(f"Passed (on the correct side of VWAP): {passed_count} ({pct(passed_count, total)})")
    blocked = total - passed_count
    print(f"Blocked (wrong side of VWAP): {blocked} ({pct(blocked, total)})")
    if distances_pct:
        distances_pct.sort()
        median_dist = distances_pct[len(distances_pct) // 2]
        very_close = sum(1 for d in distances_pct if d < 0.1)
        print()
        print(f"Of the blocked evaluations, median distance on the WRONG side of VWAP: {median_dist:.3f}%")
        print(f"Very close calls (wrong side by less than 0.1%): {very_close} ({pct(very_close, blocked)} of blocked)")


def adx_gate_analysis(today: list[dict]) -> None:
    section("4. ADX STRENGTH  (BUY needs >= 25, SELL needs >= 20)")
    margins = []
    passed_count = 0
    total = 0
    for r in today:
        adx = r.get("adx")
        direction = r.get("final_direction")
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
        print("ADX/direction fields not found for this date.")
        return
    print(f"Total evaluations with ADX + direction: {total}")
    print(f"Passed: {passed_count} ({pct(passed_count, total)})")
    blocked = total - passed_count
    print(f"Blocked: {blocked} ({pct(blocked, total)})")
    if margins:
        margins.sort()
        median_short = margins[len(margins) // 2]
        very_close = sum(1 for m in margins if m < 2.0)
        print()
        print(f"Of the blocked evaluations, median shortfall below the threshold: {median_short:.1f} ADX points")
        print(f"Very close calls (within 2 ADX points of passing): {very_close} ({pct(very_close, blocked)} of blocked)")


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

    section(f"FILTER STRICTNESS ANALYSIS -- {date_str}")
    print(f"Total evaluations: {len(today)}")

    confirmation_count_analysis(today)
    cost_gate_analysis(today)
    vwap_gate_analysis(today)
    adx_gate_analysis(today)


if __name__ == "__main__":
    main()
