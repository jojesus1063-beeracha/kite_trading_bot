#!/usr/bin/env python3

import json
import gzip
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from statistics import median

RESET = datetime.fromisoformat("2026-08-26T10:55:09")

TRADES = Path("trade_history.jsonl")
TICKS = Path(
    "runtime/equity_socket_shadow/"
    "ticks_2026-08-26.jsonl.gz"
)

WINDOW_SEC = 30
STRONG_IMBALANCE = 0.20
PERSISTENCE_REQ = 0.70


def parse_dt(v):
    if not v:
        return None

    try:
        d = datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )
        return d.replace(tzinfo=None)
    except Exception:
        return None


def depth_snapshot(tick):
    depth = tick.get("depth") or {}

    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    try:
        bid_qty = sum(
            max(0, float(x.get("quantity") or 0))
            for x in buys[:5]
        )

        ask_qty = sum(
            max(0, float(x.get("quantity") or 0))
            for x in sells[:5]
        )
    except Exception:
        return None

    total = bid_qty + ask_qty

    if total <= 0:
        return None

    imbalance = (bid_qty - ask_qty) / total

    return {
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "imbalance": imbalance,
    }


# ------------------------------------------------------
# ACTUAL POST-RESET TRADES
# ------------------------------------------------------

legs = []

with TRADES.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        entry_time = parse_dt(x.get("entry_time"))

        if not entry_time or entry_time < RESET:
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

        detail = x.get("entry_quality_detail") or {}

        emaatr = detail.get("ema_distance_atr")

        pnl = (
            x.get("net_pnl")
            if x.get("net_pnl") is not None
            else x.get("pnl")
        )

        try:
            entry = float(x.get("entry"))
            pnl = float(pnl or 0)
            emaatr = float(emaatr)
        except Exception:
            continue

        legs.append({
            "entry_time": entry_time,
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "emaatr": emaatr,
            "pnl": pnl,
        })


# Group scalp + runner
groups = {}

for x in legs:
    key = (
        x["entry_time"],
        x["symbol"],
        x["side"],
        x["entry"],
    )

    if key not in groups:
        groups[key] = dict(x)
        groups[key]["pnl"] = 0.0

    groups[key]["pnl"] += x["pnl"]

trades = sorted(
    groups.values(),
    key=lambda x: x["entry_time"]
)

print("Grouped post-reset trades:", len(trades))


# ------------------------------------------------------
# LOAD TICKS ONLY FOR TRADED SYMBOLS
# ------------------------------------------------------

wanted = {x["symbol"] for x in trades}

ticks = defaultdict(list)

with gzip.open(TICKS, "rt", errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        symbol = x.get("symbol")

        if symbol not in wanted:
            continue

        ts = parse_dt(x.get("exchange_timestamp"))

        if not ts or ts < RESET:
            continue

        book = depth_snapshot(x)

        if not book:
            continue

        ticks[symbol].append({
            "time": ts,
            **book,
        })

for symbol in ticks:
    ticks[symbol].sort(
        key=lambda x: x["time"]
    )

print("Symbols with usable five-level ticks:",
      len(ticks))


# ------------------------------------------------------
# ENTRY WINDOW
# ------------------------------------------------------

def analyze_depth(trade):

    observations = []

    for x in ticks.get(trade["symbol"], []):

        delta = (
            trade["entry_time"] - x["time"]
        ).total_seconds()

        if 0 <= delta <= WINDOW_SEC:
            observations.append(x)

    if not observations:
        return None

    vals = [
        x["imbalance"]
        for x in observations
    ]

    buyer_strong = sum(
        v >= STRONG_IMBALANCE for v in vals
    )

    seller_strong = sum(
        v <= -STRONG_IMBALANCE for v in vals
    )

    buyer_persistence = (
        buyer_strong / len(vals)
    )

    seller_persistence = (
        seller_strong / len(vals)
    )

    avg_imbalance = sum(vals) / len(vals)
    med_imbalance = median(vals)
    latest_imbalance = vals[-1]

    if buyer_persistence >= PERSISTENCE_REQ:
        dominance = "BUYERS"

    elif seller_persistence >= PERSISTENCE_REQ:
        dominance = "SELLERS"

    elif med_imbalance >= STRONG_IMBALANCE:
        dominance = "BUY_LEAN"

    elif med_imbalance <= -STRONG_IMBALANCE:
        dominance = "SELL_LEAN"

    else:
        dominance = "MIXED"

    aligned = (
        trade["side"] == "BUY"
        and dominance in ("BUYERS", "BUY_LEAN")
    ) or (
        trade["side"] == "SELL"
        and dominance in ("SELLERS", "SELL_LEAN")
    )

    opposite = (
        trade["side"] == "BUY"
        and dominance in ("SELLERS", "SELL_LEAN")
    ) or (
        trade["side"] == "SELL"
        and dominance in ("BUYERS", "BUY_LEAN")
    )

    return {
        "samples": len(vals),
        "avg": avg_imbalance,
        "median": med_imbalance,
        "latest": latest_imbalance,
        "buyer_persistence": buyer_persistence,
        "seller_persistence": seller_persistence,
        "dominance": dominance,
        "aligned": aligned,
        "opposite": opposite,
    }


# ------------------------------------------------------
# REPORT
# ------------------------------------------------------

print()
print("=" * 135)
print("FIVE-LEVEL DEPTH AT ACTUAL ₹5K ENTRY")
print("=" * 135)

print(
    f"{'TIME':6} "
    f"{'SYMBOL':12} "
    f"{'SIDE':5} "
    f"{'ATR':>6} "
    f"{'DEPTH':>10} "
    f"{'MED':>7} "
    f"{'AVG':>7} "
    f"{'BUY%':>7} "
    f"{'SELL%':>7} "
    f"{'ALIGN':>7} "
    f"{'PNL':>11}"
)

print("-" * 135)

results = []

for tr in trades:

    d = analyze_depth(tr)

    if d is None:

        print(
            f"{tr['entry_time'].strftime('%H:%M'):6} "
            f"{tr['symbol']:12} "
            f"{tr['side']:5} "
            f"{tr['emaatr']:6.3f} "
            f"{'NO DATA':>10} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"{'-':>7} "
            f"₹{tr['pnl']:>+9.2f}"
        )

        continue

    results.append((tr, d))

    print(
        f"{tr['entry_time'].strftime('%H:%M'):6} "
        f"{tr['symbol']:12} "
        f"{tr['side']:5} "
        f"{tr['emaatr']:6.3f} "
        f"{d['dominance']:>10} "
        f"{d['median']:7.3f} "
        f"{d['avg']:7.3f} "
        f"{d['buyer_persistence']*100:6.1f}% "
        f"{d['seller_persistence']*100:6.1f}% "
        f"{('YES' if d['aligned'] else 'NO'):>7} "
        f"₹{tr['pnl']:>+9.2f}"
    )


# ------------------------------------------------------
# SUMMARY BY ALIGNMENT
# ------------------------------------------------------

print()
print("=" * 135)
print("DEPTH ALIGNMENT PERFORMANCE")
print("=" * 135)

categories = {
    "ALIGNED": [],
    "OPPOSITE": [],
    "MIXED": [],
}

for tr, d in results:

    if d["aligned"]:
        categories["ALIGNED"].append(tr)

    elif d["opposite"]:
        categories["OPPOSITE"].append(tr)

    else:
        categories["MIXED"].append(tr)

for name, arr in categories.items():

    n = len(arr)
    wins = sum(x["pnl"] > 0 for x in arr)
    pnl = sum(x["pnl"] for x in arr)

    wr = wins / n * 100 if n else 0

    print(
        f"{name:10} "
        f"N={n:2} "
        f"W={wins:2} "
        f"WR={wr:6.2f}% "
        f"PNL=₹{pnl:+.2f}"
    )


# ------------------------------------------------------
# ATR + DEPTH
# ------------------------------------------------------

print()
print("=" * 135)
print("EMA9/ATR + DEPTH")
print("=" * 135)

buckets = defaultdict(list)

for tr, d in results:

    zone = (
        "<=0.25"
        if tr["emaatr"] <= 0.25
        else ">0.25"
    )

    if d["aligned"]:
        dep = "ALIGNED"

    elif d["opposite"]:
        dep = "OPPOSITE"

    else:
        dep = "MIXED"

    buckets[(zone, dep)].append(tr)

for zone in ("<=0.25", ">0.25"):

    for dep in (
        "ALIGNED",
        "OPPOSITE",
        "MIXED"
    ):

        arr = buckets[(zone, dep)]

        n = len(arr)
        wins = sum(
            x["pnl"] > 0 for x in arr
        )

        pnl = sum(
            x["pnl"] for x in arr
        )

        wr = (
            wins / n * 100
            if n else 0
        )

        print(
            f"{zone:8} + {dep:8} "
            f"N={n:2} "
            f"W={wins:2} "
            f"WR={wr:6.2f}% "
            f"PNL=₹{pnl:+.2f}"
        )


# ------------------------------------------------------
# SPECIAL: OLAELEC LOSS
# ------------------------------------------------------

print()
print("=" * 135)
print("OLAELEC 11:03 LOSS — DEPTH DETAIL")
print("=" * 135)

for tr, d in results:

    if (
        tr["symbol"] == "OLAELEC"
        and tr["entry_time"].strftime("%H:%M")
        == "11:03"
    ):

        print(
            "Trade side          :", tr["side"]
        )
        print(
            "EMA9 distance ATR   :",
            tr["emaatr"]
        )
        print(
            "Depth dominance     :",
            d["dominance"]
        )
        print(
            "Median imbalance    :",
            round(d["median"], 4)
        )
        print(
            "Average imbalance   :",
            round(d["avg"], 4)
        )
        print(
            "Buyer persistence   :",
            f"{d['buyer_persistence']*100:.1f}%"
        )
        print(
            "Seller persistence  :",
            f"{d['seller_persistence']*100:.1f}%"
        )
        print(
            "Depth aligned       :",
            d["aligned"]
        )
        print(
            "Net P&L             :",
            f"₹{tr['pnl']:+.2f}"
        )

print()
print("Imbalance meaning:")
print(" +1.00 = entirely bid-side visible quantity")
print("  0.00 = balanced five-level book")
print(" -1.00 = entirely ask-side visible quantity")
print()
print(
    "Current strong threshold = ±0.20, "
    "persistence requirement = 70%"
)
