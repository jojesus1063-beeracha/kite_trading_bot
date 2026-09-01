#!/usr/bin/env python3

import json
import statistics
from pathlib import Path
from collections import defaultdict

ROOT = Path(".")

BANDS = [
    (0.00, 0.10),
    (0.10, 0.20),
    (0.20, 0.25),
    (0.25, 0.30),
    (0.30, 0.40),
    (0.40, 0.50),
    (0.50, 0.75),
    (0.75, 1.00),
    (1.00, 1.50),
    (1.50, float("inf")),
]

# ---------------------------------------------------------
# Find trade-history files
# ---------------------------------------------------------

files = []

for p in ROOT.rglob("*.jsonl"):
    s = str(p).lower()

    if "trade_history" in s and "runtime/deploy_backups" not in s:
        files.append(p)

files = sorted(set(files))

print("=" * 125)
print("HISTORICAL EMA9 / ATR BAND ANALYSIS")
print("=" * 125)

print("\nTrade files:")
for p in files:
    print(" ", p)

# ---------------------------------------------------------
# Load usable trade legs
# ---------------------------------------------------------

raw = []

for p in files:
    try:
        fh = p.open(errors="replace")
    except Exception:
        continue

    with fh:
        for line in fh:
            try:
                x = json.loads(line)
            except Exception:
                continue

            q = x.get("entry_quality_detail") or {}

            emaatr = q.get("ema_distance_atr")
            if emaatr is None:
                emaatr = x.get("ema_distance_atr")

            pnl = (
                x.get("net_pnl")
                if x.get("net_pnl") is not None
                else x.get("pnl")
            )

            if emaatr is None or pnl is None:
                continue

            if x.get("entry") is None:
                continue

            try:
                emaatr = float(emaatr)
                pnl = float(pnl)
                entry = float(x.get("entry"))
            except Exception:
                continue

            raw.append({
                "date": str(x.get("date") or ""),
                "symbol": str(x.get("symbol") or ""),
                "direction": str(
                    x.get("direction")
                    or x.get("side")
                    or x.get("transaction_type")
                    or ""
                ),
                "entry": entry,
                "entry_time": str(x.get("entry_time") or ""),
                "signal_id": str(x.get("signal_id") or ""),
                "pnl": pnl,
                "emaatr": emaatr,
            })

print(f"\nRaw usable exit legs: {len(raw)}")

# ---------------------------------------------------------
# Exact deduplication
# ---------------------------------------------------------

dedup = {}

for x in raw:
    key = (
        x["date"],
        x["symbol"],
        x["direction"],
        x["entry"],
        x["entry_time"],
        x["signal_id"],
        round(x["pnl"], 6),
    )

    dedup.setdefault(key, x)

raw = list(dedup.values())

print(f"After exact deduplication: {len(raw)}")

# ---------------------------------------------------------
# Group split legs into actual entries
# ---------------------------------------------------------

groups = {}

for x in raw:
    key = (
        x["date"],
        x["symbol"],
        x["direction"],
        x["entry"],
        x["entry_time"],
        x["signal_id"],
    )

    if key not in groups:
        groups[key] = {
            "date": x["date"],
            "symbol": x["symbol"],
            "direction": x["direction"],
            "entry": x["entry"],
            "entry_time": x["entry_time"],
            "signal_id": x["signal_id"],
            "pnl": 0.0,
            "emaatr": x["emaatr"],
            "legs": 0,
        }

    groups[key]["pnl"] += x["pnl"]
    groups[key]["legs"] += 1

trades = list(groups.values())

print(f"Grouped actual entries: {len(trades)}")

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def band_name(lo, hi):
    if hi == float("inf"):
        return f">={lo:.2f}"
    return f"{lo:.2f}-{hi:.2f}"

def in_band(x, lo, hi):
    if hi == float("inf"):
        return x >= lo
    return lo <= x < hi

def stats(arr):
    if not arr:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "avg": 0.0,
            "median": 0.0,
            "best": 0.0,
            "worst": 0.0,
        }

    vals = [x["pnl"] for x in arr]

    wins = sum(v > 0 for v in vals)
    losses = sum(v <= 0 for v in vals)

    return {
        "n": len(arr),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(arr) * 100,
        "pnl": sum(vals),
        "avg": sum(vals) / len(vals),
        "median": statistics.median(vals),
        "best": max(vals),
        "worst": min(vals),
    }

def print_band_table(data, title):
    print()
    print("=" * 125)
    print(title)
    print("=" * 125)

    print(
        f"{'ATR BAND':12} "
        f"{'N':>6} "
        f"{'W':>5} "
        f"{'L':>5} "
        f"{'WIN%':>8} "
        f"{'NET PNL':>13} "
        f"{'AVG':>11} "
        f"{'MEDIAN':>11} "
        f"{'BEST':>11} "
        f"{'WORST':>11}"
    )

    print("-" * 125)

    results = []

    for lo, hi in BANDS:
        subset = [
            x for x in data
            if in_band(x["emaatr"], lo, hi)
        ]

        s = stats(subset)
        results.append((lo, hi, s))

        print(
            f"{band_name(lo,hi):12} "
            f"{s['n']:6} "
            f"{s['wins']:5} "
            f"{s['losses']:5} "
            f"{s['wr']:7.2f}% "
            f"₹{s['pnl']:>+11.2f} "
            f"₹{s['avg']:>+9.2f} "
            f"₹{s['median']:>+9.2f} "
            f"₹{s['best']:>+9.2f} "
            f"₹{s['worst']:>+9.2f}"
        )

    return results

# ---------------------------------------------------------
# Full historical band results
# ---------------------------------------------------------

full_results = print_band_table(
    trades,
    "ALL HISTORICAL DATA — EMA9/ATR BANDS"
)

# ---------------------------------------------------------
# Exclude Aug 25
# ---------------------------------------------------------

without_aug25 = [
    x for x in trades
    if x["date"] != "2026-08-25"
]

no25_results = print_band_table(
    without_aug25,
    "HISTORICAL DATA EXCLUDING 2026-08-25"
)

# ---------------------------------------------------------
# Compare each band's behavior with/without Aug25
# ---------------------------------------------------------

print()
print("=" * 125)
print("AUGUST 25 DISTORTION CHECK")
print("=" * 125)

print(
    f"{'ATR BAND':12} "
    f"{'ALL N':>7} "
    f"{'ALL PNL':>13} "
    f"{'NO25 N':>8} "
    f"{'NO25 PNL':>13} "
    f"{'AUG25 CONTRIBUTION':>20}"
)

print("-" * 90)

for (lo,hi,all_s), (_,_,no_s) in zip(full_results, no25_results):
    aug25_effect = all_s["pnl"] - no_s["pnl"]

    print(
        f"{band_name(lo,hi):12} "
        f"{all_s['n']:7} "
        f"₹{all_s['pnl']:>+11.2f} "
        f"{no_s['n']:8} "
        f"₹{no_s['pnl']:>+11.2f} "
        f"₹{aug25_effect:>+18.2f}"
    )

# ---------------------------------------------------------
# Daily band matrix
# ---------------------------------------------------------

dates = sorted(set(x["date"] for x in trades))

print()
print("=" * 125)
print("DAILY P&L BY EMA9/ATR BAND")
print("=" * 125)

header = f"{'DATE':12}"

for lo,hi in BANDS:
    header += f" {band_name(lo,hi):>11}"

print(header)
print("-" * len(header))

for date in dates:
    day = [x for x in trades if x["date"] == date]

    row = f"{date:12}"

    for lo,hi in BANDS:
        s = stats([
            x for x in day
            if in_band(x["emaatr"], lo, hi)
        ])

        row += f" {s['pnl']:>+11.0f}"

    print(row)

# ---------------------------------------------------------
# Day consistency for each band
# ---------------------------------------------------------

print()
print("=" * 125)
print("BAND CONSISTENCY BY DAY")
print("=" * 125)

print(
    f"{'ATR BAND':12} "
    f"{'ACTIVE DAYS':>12} "
    f"{'POS DAYS':>10} "
    f"{'NEG DAYS':>10} "
    f"{'POS DAY %':>11} "
    f"{'TOTAL PNL':>13}"
)

print("-" * 80)

for lo,hi in BANDS:
    active = 0
    positive = 0
    negative = 0
    total = 0.0

    for date in dates:
        subset = [
            x for x in trades
            if x["date"] == date
            and in_band(x["emaatr"], lo, hi)
        ]

        if not subset:
            continue

        active += 1
        pnl = sum(x["pnl"] for x in subset)
        total += pnl

        if pnl > 0:
            positive += 1
        elif pnl < 0:
            negative += 1

    pos_pct = positive / active * 100 if active else 0

    print(
        f"{band_name(lo,hi):12} "
        f"{active:12} "
        f"{positive:10} "
        f"{negative:10} "
        f"{pos_pct:10.2f}% "
        f"₹{total:>+11.2f}"
    )

# ---------------------------------------------------------
# Inspect the suspicious 0.40-0.50 zone
# ---------------------------------------------------------

focus = [
    x for x in trades
    if 0.40 <= x["emaatr"] < 0.50
]

print()
print("=" * 125)
print("DETAIL — 0.40 TO 0.50 ATR TRADES")
print("=" * 125)

print(
    f"{'DATE':12} "
    f"{'TIME':26} "
    f"{'SYMBOL':15} "
    f"{'DIR':5} "
    f"{'EMAATR':>8} "
    f"{'PNL':>12}"
)

print("-" * 90)

for x in sorted(
    focus,
    key=lambda z: (
        z["date"],
        z["entry_time"],
        z["symbol"]
    )
):
    print(
        f"{x['date']:12} "
        f"{x['entry_time'][:26]:26} "
        f"{x['symbol']:15} "
        f"{x['direction'][:5]:5} "
        f"{x['emaatr']:8.4f} "
        f"₹{x['pnl']:>+10.2f}"
    )

print()
print("=" * 125)
print("INTERPRETATION")
print("=" * 125)
print("Look for BANDS, not one magic threshold.")
print("A useful band should ideally have:")
print(" - positive or near-positive average P&L")
print(" - reasonable sample size")
print(" - positive behavior across multiple days")
print(" - no dependence on one huge winning trade")
print(" - similar behavior after excluding Aug 25")
print("=" * 125)
