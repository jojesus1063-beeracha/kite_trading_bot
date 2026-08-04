"""
WS shadow-mode log review.

Reads ws_shadow_logs/*.jsonl (written by candle_engine.ShadowComparator
and indicators_incremental.IncrementalShadowComparator when
cfg.ENABLE_WS_CANDLES=True) and produces a summary of how closely the
WS-built candles/indicators matched the existing REST path.

Pure analytics -- reads log files, never touches trading behavior,
never calls Kite. Safe to run any time, including while the bot is
live in shadow mode.

This is the tool to run after 2-3 shadow sessions, BEFORE ever
considering cfg.WS_CANDLE_MODE = "live".

Usage:
    python3 review_ws_shadow_logs.py                  # today's logs
    python3 review_ws_shadow_logs.py 2026-08-05        # a specific date
    python3 review_ws_shadow_logs.py --all             # every log file present
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from glob import glob

LOG_DIR = "ws_shadow_logs"


def _load_jsonl(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partially-written last line during a crash -- skip, don't crash the report
    return records


def load_candle_log(iso_date):
    return _load_jsonl(os.path.join(LOG_DIR, f"ws_shadow_{iso_date}.jsonl"))


def load_indicator_log(iso_date):
    return _load_jsonl(os.path.join(LOG_DIR, f"ws_shadow_indicators_{iso_date}.jsonl"))


def summarize_candle_log(records):
    """
    Returns per-symbol stats: comparisons made, how many were within
    tolerance, and the worst mismatches seen (for eyeballing whether a
    mismatch is a rounding artifact or a real bug).
    """
    by_symbol = defaultdict(lambda: {"compared": 0, "within_tolerance": 0,
                                      "no_rest_data": 0, "no_matching_candle": 0,
                                      "worst_mismatches": []})

    for r in records:
        symbol = r.get("symbol", "UNKNOWN")
        stats = by_symbol[symbol]
        status = r.get("status")
        if status == "no_rest_data":
            stats["no_rest_data"] += 1
            continue
        if status == "no_matching_rest_candle":
            stats["no_matching_candle"] += 1
            continue
        if status != "compared":
            continue

        stats["compared"] += 1
        if r.get("within_tolerance"):
            stats["within_tolerance"] += 1
        else:
            worst_field, worst_delta = None, 0.0
            for field_name in ("open", "high", "low", "close"):
                delta = r.get(f"{field_name}_delta")
                if delta is not None and delta > worst_delta:
                    worst_field, worst_delta = field_name, delta
            vol_delta = r.get("volume_delta_pct", 0.0)
            stats["worst_mismatches"].append({
                "date": r.get("date"), "worst_field": worst_field, "worst_delta": worst_delta,
                "volume_delta_pct": vol_delta,
            })

    return by_symbol


def summarize_indicator_log(records):
    by_symbol_tf = defaultdict(lambda: {"compared": 0, "within_tolerance": 0, "worst_mismatches": []})

    for r in records:
        key = (r.get("symbol", "UNKNOWN"), r.get("timeframe", "?"))
        stats = by_symbol_tf[key]
        stats["compared"] += 1
        if r.get("within_tolerance"):
            stats["within_tolerance"] += 1
        else:
            mismatched_fields = {
                k: v for k, v in r.items()
                if isinstance(v, dict) and v.get("delta") is not None and v["delta"] > 0
            }
            stats["worst_mismatches"].append({"logged_at": r.get("logged_at"), "fields": mismatched_fields})

    return by_symbol_tf


def format_report(iso_date):
    candle_records = load_candle_log(iso_date)
    indicator_records = load_indicator_log(iso_date)

    lines = []
    lines.append(f"=== WS Shadow-Mode Report: {iso_date} ===")
    lines.append("")

    if not candle_records and not indicator_records:
        lines.append("No shadow logs found for this date.")
        lines.append("(Either the bot wasn't run with ENABLE_WS_CANDLES=True that day, "
                      "or ws_shadow_logs/ isn't in the working directory this script was run from.)")
        return "\n".join(lines)

    # -- Candle comparison summary -----------------------------------------
    lines.append("--- 5-Minute Candle Comparison (WS vs REST) ---")
    candle_summary = summarize_candle_log(candle_records)
    if not candle_summary:
        lines.append("No candle comparisons logged.")
    else:
        total_compared = sum(s["compared"] for s in candle_summary.values())
        total_ok = sum(s["within_tolerance"] for s in candle_summary.values())
        overall_pct = (total_ok / total_compared * 100) if total_compared else 0
        lines.append(f"Overall: {total_ok}/{total_compared} candles within tolerance ({overall_pct:.1f}%)")
        lines.append("")
        for symbol in sorted(candle_summary):
            s = candle_summary[symbol]
            if s["compared"] == 0 and s["no_rest_data"] == 0 and s["no_matching_candle"] == 0:
                continue
            if s["compared"] == 0:
                lines.append(f"  {symbol}: no successful comparisons "
                             f"({s['no_rest_data']} no REST data, {s['no_matching_candle']} no matching candle)")
                continue
            pct = (s["within_tolerance"] / s["compared"] * 100)
            flag = "" if pct == 100 else "  <-- MISMATCHES, review before trusting this symbol's WS candles"
            lines.append(f"  {symbol}: {s['within_tolerance']}/{s['compared']} within tolerance ({pct:.1f}%){flag}")
            if s["no_rest_data"] or s["no_matching_candle"]:
                lines.append(f"    (also skipped: {s['no_rest_data']} no REST data, {s['no_matching_candle']} no matching candle)")
            for m in s["worst_mismatches"][:3]:
                lines.append(f"    mismatch at {m['date']}: worst field={m['worst_field']} "
                             f"delta={m['worst_delta']:.4f}, volume_delta={m['volume_delta_pct']:.2f}%")
            if len(s["worst_mismatches"]) > 3:
                lines.append(f"    ...and {len(s['worst_mismatches']) - 3} more mismatch(es)")

    lines.append("")

    # -- Indicator comparison summary ----------------------------------------
    lines.append("--- Indicator Comparison (incremental vs batch recompute) ---")
    indicator_summary = summarize_indicator_log(indicator_records)
    if not indicator_summary:
        lines.append("No indicator comparisons logged.")
    else:
        for (symbol, timeframe) in sorted(indicator_summary):
            s = indicator_summary[(symbol, timeframe)]
            pct = (s["within_tolerance"] / s["compared"] * 100) if s["compared"] else 0
            flag = "" if pct == 100 else "  <-- MISMATCHES, review before trusting this symbol's WS indicators"
            lines.append(f"  {symbol} ({timeframe}): {s['within_tolerance']}/{s['compared']} within tolerance ({pct:.1f}%){flag}")
            for m in s["worst_mismatches"][:3]:
                field_summary = ", ".join(
                    f"{k}: inc={v['incremental']:.4f} batch={v['batch']:.4f} delta={v['delta']:.4f}"
                    for k, v in m["fields"].items()
                )
                lines.append(f"    at {m['logged_at']}: {field_summary}")

    lines.append("")
    lines.append("--- Verdict ---")
    total_candle_compared = sum(s["compared"] for s in candle_summary.values()) if candle_summary else 0
    total_candle_ok = sum(s["within_tolerance"] for s in candle_summary.values()) if candle_summary else 0
    total_ind_compared = sum(s["compared"] for s in indicator_summary.values()) if indicator_summary else 0
    total_ind_ok = sum(s["within_tolerance"] for s in indicator_summary.values()) if indicator_summary else 0

    if total_candle_compared == 0 and total_ind_compared == 0:
        lines.append("Not enough data yet -- run at least one more full session before deciding anything.")
    elif total_candle_ok == total_candle_compared and total_ind_ok == total_ind_compared:
        lines.append("All comparisons within tolerance for this date. Still recommended: review "
                     "2-3 separate sessions (not just this one) before considering WS_CANDLE_MODE='live'.")
    else:
        lines.append("Mismatches found -- do NOT consider WS_CANDLE_MODE='live' yet. "
                      "Review the flagged symbols above; a mismatch concentrated in one illiquid "
                      "symbol is a different concern than one spread across the whole watchlist.")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        dates = sorted({
            os.path.basename(p).replace("ws_shadow_", "").replace("ws_shadow_indicators_", "").replace(".jsonl", "")
            for p in glob(os.path.join(LOG_DIR, "*.jsonl"))
        })
        if not dates:
            print(f"No log files found in {LOG_DIR}/")
        for d in dates:
            print(format_report(d))
            print()
    else:
        iso_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
        print(format_report(iso_date))
