#!/usr/bin/env python3

import json
from pathlib import Path
from collections import defaultdict

ROOT = Path(".")
THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35,
              0.40, 0.50, 0.75, 1.00, 1.50, 2.00]

# ---------------------------------------------------------
# Find historical trade files
# ---------------------------------------------------------
files = []

for p in ROOT.rglob("*.jsonl"):
    s = str(p).lower()

    if "trade_history" in s and "runtime/deploy_backups" not in s:
        files.append(p)

files = sorted(set(files))

print("=" * 110)
print("HISTORICAL EMA9 / ATR ENTRY-GATE REPLAY")
print("=" * 110)

print("\nTrade files found:")
for f in files:
    print(" ", f)

if not files:
    raise SystemExit("No trade_history JSONL files found.")

# ---------------------------------------------------------
# Load trade legs
# ---------------------------------------------------------
raw = []

for file in files:
    try:
        fh = file.open(errors="replace")
    except:
        continue

    with fh:
        for line in fh:
            try:
                x = json.loads(line)
            except:
                continue

            q = x.get("entry_quality_detail") or {}

            emaatr = q.get("ema_distance_atr")

            # fallback in case older records stored it top-level
            if emaatr is None:
                emaatr = x.get("ema_distance_atr")

            if emaatr is None:
                continue

            pnl = (
                x.get("net_pnl")
                if x.get("net_pnl") is not None
                else x.get("pnl")
            )

            if pnl is None:
                continue

            symbol = x.get("symbol")
            direction = (
                x.get("direction")
                or x.get("side")
                or x.get("transaction_type")
            )

            entry = x.get("entry")
            entry_time = x.get("entry_time")
            date = x.get("date")
            signal_id = x.get("signal_id")

            if not symbol or entry is None:
                continue

            try:
                emaatr = float(emaatr)
                pnl = float(pnl)
                entry = float(entry)
            except:
                continue

            raw.append({
                "file": str(file),
                "date": str(date or ""),
                "symbol": str(symbol),
                "direction": str(direction or ""),
                "entry": entry,
                "entry_time": str(entry_time or ""),
                "signal_id": str(signal_id or ""),
                "emaatr": emaatr,
                "pnl": pnl,
            })

print(f"\nUsable exit legs with EMA-distance data: {len(raw)}")

# ---------------------------------------------------------
# Remove exact duplicate records caused by backup/history files
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

    if key not in dedup:
        dedup[key] = x

raw = list(dedup.values())

print(f"After exact deduplication              : {len(raw)}")

# ---------------------------------------------------------
# Group scalp/runner legs into ONE entry
# ---------------------------------------------------------
groups = {}

for x in raw:

    # signal_id is best when present.
    # entry_time + symbol + direction + price provides fallback.
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
            "emaatr": x["emaatr"],
            "pnl": 0.0,
            "legs": 0,
        }

    groups[key]["pnl"] += x["pnl"]
    groups[key]["legs"] += 1

trades = list(groups.values())

print(f"Actual grouped entries                : {len(trades)}")

if not trades:
    raise SystemExit("No usable grouped historical trades.")

# ---------------------------------------------------------
# Statistics
# ---------------------------------------------------------
def stats(arr):
    if not arr:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0,
            "wr": 0,
            "avg": 0,
        }

    wins = sum(x["pnl"] > 0 for x in arr)
    losses = sum(x["pnl"] <= 0 for x in arr)
    pnl = sum(x["pnl"] for x in arr)

    return {
        "n": len(arr),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "wr": wins / len(arr) * 100,
        "avg": pnl / len(arr),
    }

base = stats(trades)

print()
print("=" * 110)
print("ALL HISTORICAL TRADES")
print("=" * 110)

print(f"Trades        : {base['n']}")
print(f"Wins/Losses   : {base['wins']} / {base['losses']}")
print(f"Win rate      : {base['wr']:.2f}%")
print(f"Net P&L       : ₹{base['pnl']:+.2f}")
print(f"Average/trade : ₹{base['avg']:+.2f}")

# ---------------------------------------------------------
# Threshold replay
# ---------------------------------------------------------
print()
print("=" * 110)
print("EMA9 DISTANCE / ATR THRESHOLD REPLAY")
print("=" * 110)

print(
    f"{'THRESHOLD':>10} "
    f"{'TRADES':>8} "
    f"{'WINS':>7} "
    f"{'LOSS':>7} "
    f"{'WIN%':>8} "
    f"{'NET PNL':>14} "
    f"{'AVG/TRADE':>12} "
    f"{'VS BASE':>14}"
)

print("-" * 110)

results = []

for threshold in THRESHOLDS:

    kept = [
        x for x in trades
        if x["emaatr"] <= threshold
    ]

    s = stats(kept)

    delta = s["pnl"] - base["pnl"]

    results.append((threshold, s, delta))

    print(
        f"{threshold:10.2f} "
        f"{s['n']:8} "
        f"{s['wins']:7} "
        f"{s['losses']:7} "
        f"{s['wr']:7.2f}% "
        f"₹{s['pnl']:>+12.2f} "
        f"₹{s['avg']:>+10.2f} "
        f"₹{delta:>+12.2f}"
    )

# ---------------------------------------------------------
# Daily consistency
# ---------------------------------------------------------
print()
print("=" * 110)
print("DAILY RESULTS — CURRENT vs <=0.25 ATR")
print("=" * 110)

by_date = defaultdict(list)

for x in trades:
    by_date[x["date"]].append(x)

print(
    f"{'DATE':12} "
    f"{'ALL':>7} "
    f"{'BASE PNL':>12} "
    f"{'<=.25':>7} "
    f"{'.25 PNL':>12} "
    f"{'DELTA':>12}"
)

print("-" * 75)

positive_delta_days = 0
negative_delta_days = 0
flat_days = 0

for date in sorted(by_date):

    day = by_date[date]

    b = stats(day)

    filtered = [
        x for x in day
        if x["emaatr"] <= 0.25
    ]

    f = stats(filtered)

    delta = f["pnl"] - b["pnl"]

    if delta > 0:
        positive_delta_days += 1
    elif delta < 0:
        negative_delta_days += 1
    else:
        flat_days += 1

    print(
        f"{date:12} "
        f"{b['n']:7} "
        f"₹{b['pnl']:>+10.2f} "
        f"{f['n']:7} "
        f"₹{f['pnl']:>+10.2f} "
        f"₹{delta:>+10.2f}"
    )

print()
print("Days where <=0.25 improved P&L :", positive_delta_days)
print("Days where <=0.25 reduced P&L  :", negative_delta_days)
print("Days unchanged                  :", flat_days)

# ---------------------------------------------------------
# <=0.25 versus >0.25 populations
# ---------------------------------------------------------
near = [x for x in trades if x["emaatr"] <= 0.25]
far  = [x for x in trades if x["emaatr"] > 0.25]

ns = stats(near)
fs = stats(far)

print()
print("=" * 110)
print("NEAR EMA9 vs EXTENDED")
print("=" * 110)

print(
    f"<=0.25 ATR : trades={ns['n']} "
    f"winrate={ns['wr']:.2f}% "
    f"pnl=₹{ns['pnl']:+.2f} "
    f"avg=₹{ns['avg']:+.2f}"
)

print(
    f"> 0.25 ATR : trades={fs['n']} "
    f"winrate={fs['wr']:.2f}% "
    f"pnl=₹{fs['pnl']:+.2f} "
    f"avg=₹{fs['avg']:+.2f}"
)

# ---------------------------------------------------------
# Best threshold
# ---------------------------------------------------------
valid = [
    r for r in results
    if r[1]["n"] >= 5
]

if valid:
    best_pnl = max(valid, key=lambda r: r[1]["pnl"])
    best_avg = max(valid, key=lambda r: r[1]["avg"])

    print()
    print("=" * 110)
    print("BEST HISTORICAL THRESHOLDS")
    print("=" * 110)

    print(
        f"Highest total P&L : <= {best_pnl[0]:.2f} ATR "
        f"| trades={best_pnl[1]['n']} "
        f"| P&L=₹{best_pnl[1]['pnl']:+.2f}"
    )

    print(
        f"Highest avg/trade : <= {best_avg[0]:.2f} ATR "
        f"| trades={best_avg[1]['n']} "
        f"| avg=₹{best_avg[1]['avg']:+.2f}"
    )

print()
print("=" * 110)
print("IMPORTANT")
print("=" * 110)
print("This is a historical counterfactual filter:")
print("existing trades with EMA9-distance > threshold are removed.")
print()
print("It does NOT assume blocked capital is redeployed into another trade.")
print("It also does NOT prove 0.25 ATR is optimal.")
print()
print("We want a broad profitable region such as 0.20-0.40 ATR")
print("across multiple days, rather than one isolated best threshold.")
print("=" * 110)
