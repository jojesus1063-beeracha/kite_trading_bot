#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

RESET = datetime.fromisoformat("2026-08-26T10:55:09")

TRADES = Path("trade_history.jsonl")
EVENTS = Path("runtime/equity_socket_shadow/events_2026-08-26.jsonl")

def dt(v):
    if not v:
        return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x.replace(tzinfo=None)
    except:
        return None

def get_nested(obj, *paths):
    for path in paths:
        cur = obj
        ok = True

        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]

        if ok and cur is not None:
            return cur

    return None

# ----------------------------------------------------------
# LOAD/GROUP POST-RESET TRADES
# ----------------------------------------------------------

legs = []

with TRADES.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except:
            continue

        t = dt(x.get("entry_time"))

        if not t or t < RESET:
            continue

        symbol = x.get("symbol")

        side = str(
            x.get("direction")
            or x.get("side")
            or x.get("transaction_type")
            or ""
        ).upper()

        if not symbol or side not in ("BUY", "SELL"):
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

        try:
            entry = float(x.get("entry"))
            pnl = float(pnl or 0)
            emaatr = float(emaatr) if emaatr is not None else None
        except:
            continue

        legs.append({
            "time": t,
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "emaatr": emaatr,
            "pnl": pnl,
        })

groups = {}

for x in legs:
    key = (
        x["time"],
        x["symbol"],
        x["side"],
        x["entry"],
    )

    if key not in groups:
        groups[key] = dict(x)
        groups[key]["pnl"] = 0.0

    groups[key]["pnl"] += x["pnl"]

trades = sorted(groups.values(), key=lambda x: x["time"])

print("Grouped post-reset trades:", len(trades))

# ----------------------------------------------------------
# LOAD DEPTH EVENTS
# ----------------------------------------------------------

depth = defaultdict(list)

if not EVENTS.exists():
    raise SystemExit(f"Missing {EVENTS}")

with EVENTS.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except:
            continue

        symbol = x.get("symbol")

        t = dt(
            x.get("exchange_timestamp")
            or x.get("timestamp")
            or x.get("time")
        )

        if not symbol or not t:
            continue

        bid = get_nested(
            x,
            "bid_qty",
            "book.bid_qty",
            "depth.bid_qty",
        )

        ask = get_nested(
            x,
            "ask_qty",
            "book.ask_qty",
            "depth.ask_qty",
        )

        imbalance = get_nested(
            x,
            "book_imbalance",
            "book.book_imbalance",
            "depth.book_imbalance",
        )

        try:
            if imbalance is None and bid is not None and ask is not None:
                bid = float(bid)
                ask = float(ask)

                total = bid + ask

                if total > 0:
                    imbalance = (bid - ask) / total

            if imbalance is None:
                continue

            imbalance = float(imbalance)

        except:
            continue

        depth[symbol].append({
            "time": t,
            "imbalance": imbalance,
            "bid": bid,
            "ask": ask,
        })

for symbol in depth:
    depth[symbol].sort(key=lambda x: x["time"])

print("Symbols with depth:", len(depth))

# ----------------------------------------------------------
# FIND DEPTH BEFORE ENTRY
#
# Look at previous 30 seconds.
# This matches the paper depth-confirmation window.
# ----------------------------------------------------------

def entry_depth(tr):
    observations = []

    for d in depth.get(tr["symbol"], []):
        seconds = (tr["time"] - d["time"]).total_seconds()

        if 0 <= seconds <= 30:
            observations.append(d)

    if not observations:
        return None

    values = [x["imbalance"] for x in observations]

    avg = sum(values) / len(values)
    latest = observations[-1]["imbalance"]

    buyer_fraction = sum(v >= 0.20 for v in values) / len(values)
    seller_fraction = sum(v <= -0.20 for v in values) / len(values)

    if buyer_fraction >= 0.70:
        dominance = "BUYERS"

    elif seller_fraction >= 0.70:
        dominance = "SELLERS"

    elif avg > 0:
        dominance = "BUY_LEAN"

    elif avg < 0:
        dominance = "SELL_LEAN"

    else:
        dominance = "NEUTRAL"

    if tr["side"] == "BUY":
        aligned = dominance in ("BUYERS", "BUY_LEAN")
    else:
        aligned = dominance in ("SELLERS", "SELL_LEAN")

    return {
        "n": len(values),
        "avg": avg,
        "latest": latest,
        "buyer_fraction": buyer_fraction,
        "seller_fraction": seller_fraction,
        "dominance": dominance,
        "aligned": aligned,
    }

# ----------------------------------------------------------
# REPORT
# ----------------------------------------------------------

print()
print("=" * 125)
print("DEPTH DOMINANCE AT ACTUAL ENTRY")
print("=" * 125)

print(
    f"{'TIME':6} "
    f"{'SYMBOL':12} "
    f"{'TRADE':5} "
    f"{'EMAATR':>7} "
    f"{'DEPTH':>11} "
    f"{'AVG IMB':>9} "
    f"{'BUY%':>7} "
    f"{'SELL%':>7} "
    f"{'ALIGN':>7} "
    f"{'PNL':>11}"
)

print("-" * 125)

results = []

for tr in trades:

    d = entry_depth(tr)

    if d is None:
        print(
            f"{tr['time'].strftime('%H:%M'):6} "
            f"{tr['symbol']:12} "
            f"{tr['side']:5} "
            f"{(tr['emaatr'] or 0):7.3f} "
            f"{'NO DATA':>11} "
            f"{'-':>9} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"₹{tr['pnl']:>+9.2f}"
        )
        continue

    results.append((tr,d))

    print(
        f"{tr['time'].strftime('%H:%M'):6} "
        f"{tr['symbol']:12} "
        f"{tr['side']:5} "
        f"{(tr['emaatr'] or 0):7.3f} "
        f"{d['dominance']:>11} "
        f"{d['avg']:9.3f} "
        f"{d['buyer_fraction']*100:6.1f}% "
        f"{d['seller_fraction']*100:6.1f}% "
        f"{('YES' if d['aligned'] else 'NO'):>7} "
        f"₹{tr['pnl']:>+9.2f}"
    )

# ----------------------------------------------------------
# COMBINATION ANALYSIS
# ----------------------------------------------------------

print()
print("=" * 125)
print("EMA9/ATR + DEPTH COMBINATION")
print("=" * 125)

buckets = defaultdict(list)

for tr,d in results:

    zone = "<=0.25" if tr["emaatr"] <= 0.25 else ">0.25"
    alignment = "ALIGNED" if d["aligned"] else "OPPOSITE"

    buckets[(zone, alignment)].append(tr["pnl"])

for zone in ("<=0.25", ">0.25"):
    for alignment in ("ALIGNED", "OPPOSITE"):

        vals = buckets[(zone, alignment)]

        n = len(vals)
        wins = sum(v > 0 for v in vals)
        pnl = sum(vals)

        wr = wins / n * 100 if n else 0

        print(
            f"{zone:8} + {alignment:8} "
            f"N={n:2} "
            f"W={wins:2} "
            f"WR={wr:6.2f}% "
            f"PNL=₹{pnl:+.2f}"
        )

print()
print("=" * 125)
print("WINNERS vs LOSERS — DEPTH")
print("=" * 125)

winner_imb = [
    d["avg"]
    for tr,d in results
    if tr["pnl"] > 0
]

loser_imb = [
    d["avg"]
    for tr,d in results
    if tr["pnl"] <= 0
]

if winner_imb:
    print(
        "Winner average raw imbalance :",
        round(sum(winner_imb)/len(winner_imb), 4)
    )

if loser_imb:
    print(
        "Loser average raw imbalance  :",
        round(sum(loser_imb)/len(loser_imb), 4)
    )

print()
print("Positive imbalance = BUY pressure")
print("Negative imbalance = SELL pressure")
print("Absolute >= 0.20 = strong visible-book imbalance")
print("70% persistence = current paper confirmation requirement")
