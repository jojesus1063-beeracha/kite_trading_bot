#!/usr/bin/env python3

import gzip
import json
from pathlib import Path
from datetime import datetime, time
from collections import defaultdict
from statistics import median, mean

DATE = "2026-08-27"

TICK_FILE = Path(
    f"runtime/equity_socket_shadow/ticks_{DATE}_recovered.jsonl"
)

# Morning Top-120 session.
# Stop before the Top-30 rebuild began.
START_TIME = time(9, 15, 0)
END_TIME   = time(11, 47, 0)

# We want the latest meaningful view of each stock
# from the original Top-120 session.
FINAL_WINDOW_MINUTES = 10

# Same strong-imbalance concept used in our depth work.
STRONG = 0.20
EXTREME = 0.60
SLIGHT = 0.05


def parse_dt(value):
    if not value:
        return None

    try:
        d = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        # Logs normally contain IST-aware timestamps.
        # Keep local clock representation.
        if d.tzinfo is not None:
            d = d.astimezone().replace(tzinfo=None)

        return d
    except Exception:
        return None


def extract_timestamp(row):
    for key in (
        "timestamp",
        "exchange_timestamp",
        "last_trade_time",
        "received_at",
        "ts",
        "time",
    ):
        d = parse_dt(row.get(key))
        if d:
            return d
    return None


def extract_symbol(row):
    for key in (
        "symbol",
        "tradingsymbol",
        "trading_symbol",
    ):
        v = row.get(key)
        if v:
            return str(v)
    return None


def extract_depth(row):
    """
    Supports both:
      row['depth']['buy'/'sell']
    and already-computed bid_qty/ask_qty fields.
    """

    # First use already-computed quantities if available.
    bid_qty = row.get("bid_qty")
    ask_qty = row.get("ask_qty")

    if bid_qty is not None and ask_qty is not None:
        try:
            return float(bid_qty), float(ask_qty)
        except Exception:
            pass

    depth = row.get("depth")

    if not isinstance(depth, dict):
        return None

    bids = depth.get("buy") or depth.get("bids") or []
    asks = depth.get("sell") or depth.get("asks") or []

    def total_qty(levels):
        total = 0.0

        for x in levels[:5]:
            if not isinstance(x, dict):
                continue

            q = (
                x.get("quantity")
                if x.get("quantity") is not None
                else x.get("qty", 0)
            )

            try:
                total += float(q or 0)
            except Exception:
                pass

        return total

    return total_qty(bids), total_qty(asks)


def classify(imbalance):
    if imbalance >= EXTREME:
        return "EXTREME BUYERS"

    if imbalance >= STRONG:
        return "STRONG BUYERS"

    if imbalance >= SLIGHT:
        return "SLIGHT BUYERS"

    if imbalance > -SLIGHT:
        return "BALANCED"

    if imbalance > -STRONG:
        return "SLIGHT SELLERS"

    if imbalance > -EXTREME:
        return "STRONG SELLERS"

    return "EXTREME SELLERS"


if not TICK_FILE.exists():
    raise SystemExit(
        f"ERROR: tick file not found: {TICK_FILE}"
    )


# ---------------------------------------------------------
# Load morning records
# ---------------------------------------------------------

records = defaultdict(list)

total_rows = 0
usable_rows = 0

with TICK_FILE.open("r", encoding="utf-8") as f:

    for line in f:
        total_rows += 1

        try:
            row = json.loads(line)
        except Exception:
            continue

        dt = extract_timestamp(row)
        symbol = extract_symbol(row)

        if not dt or not symbol:
            continue

        if dt.date().isoformat() != DATE:
            continue

        if not (START_TIME <= dt.time() <= END_TIME):
            continue

        depth = extract_depth(row)

        if not depth:
            continue

        bid_qty, ask_qty = depth
        total = bid_qty + ask_qty

        if total <= 0:
            continue

        imbalance = (bid_qty - ask_qty) / total

        records[symbol].append(
            {
                "dt": dt,
                "bid_qty": bid_qty,
                "ask_qty": ask_qty,
                "imbalance": imbalance,
            }
        )

        usable_rows += 1


print("=" * 125)
print("AUG 27 MORNING TOP-120 FIVE-LEVEL DEPTH ANALYSIS")
print("=" * 125)

print("Tick file             :", TICK_FILE)
print("Rows scanned          :", f"{total_rows:,}")
print("Usable morning ticks  :", f"{usable_rows:,}")
print("Symbols found         :", len(records))
print(
    "Morning period        :",
    START_TIME.strftime("%H:%M"),
    "to",
    END_TIME.strftime("%H:%M"),
)

print()


# ---------------------------------------------------------
# For each symbol use its final 10-minute window
# ---------------------------------------------------------

results = []

for symbol, rows in records.items():

    rows.sort(key=lambda x: x["dt"])

    last_dt = rows[-1]["dt"]

    cutoff_seconds = FINAL_WINDOW_MINUTES * 60

    window = [
        x for x in rows
        if (last_dt - x["dt"]).total_seconds()
        <= cutoff_seconds
    ]

    if not window:
        continue

    imbalances = [x["imbalance"] for x in window]

    med = median(imbalances)
    avg = mean(imbalances)

    avg_bid = mean(x["bid_qty"] for x in window)
    avg_ask = mean(x["ask_qty"] for x in window)

    buyer_fraction = (
        sum(x >= STRONG for x in imbalances)
        / len(imbalances)
    )

    seller_fraction = (
        sum(x <= -STRONG for x in imbalances)
        / len(imbalances)
    )

    # Dominance ratio for easy interpretation.
    if avg_ask > 0:
        bid_ask_ratio = avg_bid / avg_ask
    else:
        bid_ask_ratio = float("inf")

    category = classify(med)

    results.append(
        {
            "symbol": symbol,
            "category": category,
            "median": med,
            "average": avg,
            "avg_bid": avg_bid,
            "avg_ask": avg_ask,
            "ratio": bid_ask_ratio,
            "buyer_persistence": buyer_fraction,
            "seller_persistence": seller_fraction,
            "samples": len(window),
            "last_time": last_dt,
        }
    )


# ---------------------------------------------------------
# Category counts
# ---------------------------------------------------------

category_order = [
    "EXTREME BUYERS",
    "STRONG BUYERS",
    "SLIGHT BUYERS",
    "BALANCED",
    "SLIGHT SELLERS",
    "STRONG SELLERS",
    "EXTREME SELLERS",
]

counts = {
    c: sum(r["category"] == c for r in results)
    for c in category_order
}


print("=" * 125)
print("CATEGORY COUNTS")
print("=" * 125)

for c in category_order:
    print(f"{c:<20} : {counts[c]:>3}")

print("-" * 40)
print(f"{'TOTAL':<20} : {len(results):>3}")


# ---------------------------------------------------------
# Full ranked table
# ---------------------------------------------------------

print()
print("=" * 125)
print("ALL STOCKS — BUYER HEAVY TO SELLER HEAVY")
print("=" * 125)

print(
    f"{'RK':>3} "
    f"{'SYMBOL':<16} "
    f"{'CATEGORY':<20} "
    f"{'MED':>8} "
    f"{'AVG':>8} "
    f"{'AVG BID':>12} "
    f"{'AVG ASK':>12} "
    f"{'B/A':>8} "
    f"{'BUY%':>8} "
    f"{'SELL%':>8} "
    f"{'N':>7}"
)

print("-" * 125)

ranked = sorted(
    results,
    key=lambda r: r["median"],
    reverse=True,
)

for i, r in enumerate(ranked, 1):

    ratio = (
        "INF"
        if r["ratio"] == float("inf")
        else f"{r['ratio']:.2f}"
    )

    print(
        f"{i:>3} "
        f"{r['symbol']:<16} "
        f"{r['category']:<20} "
        f"{r['median']:>+8.3f} "
        f"{r['average']:>+8.3f} "
        f"{r['avg_bid']:>12,.0f} "
        f"{r['avg_ask']:>12,.0f} "
        f"{ratio:>8} "
        f"{r['buyer_persistence']*100:>7.1f}% "
        f"{r['seller_persistence']*100:>7.1f}% "
        f"{r['samples']:>7}"
    )


# ---------------------------------------------------------
# Strongest buyers
# ---------------------------------------------------------

print()
print("=" * 125)
print("TOP 20 BUYER-DOMINATED STOCKS")
print("=" * 125)

buyers = sorted(
    results,
    key=lambda r: (
        r["median"],
        r["buyer_persistence"],
    ),
    reverse=True,
)[:20]

for i, r in enumerate(buyers, 1):

    print(
        f"{i:02d}. "
        f"{r['symbol']:<15} "
        f"imbalance={r['median']:+.3f} "
        f"buyer_persistence="
        f"{r['buyer_persistence']*100:5.1f}% "
        f"avg_bid={r['avg_bid']:,.0f} "
        f"avg_ask={r['avg_ask']:,.0f}"
    )


# ---------------------------------------------------------
# Strongest sellers
# ---------------------------------------------------------

print()
print("=" * 125)
print("TOP 20 SELLER-DOMINATED STOCKS")
print("=" * 125)

sellers = sorted(
    results,
    key=lambda r: (
        r["median"],
        -r["seller_persistence"],
    ),
)[:20]

for i, r in enumerate(sellers, 1):

    print(
        f"{i:02d}. "
        f"{r['symbol']:<15} "
        f"imbalance={r['median']:+.3f} "
        f"seller_persistence="
        f"{r['seller_persistence']*100:5.1f}% "
        f"avg_bid={r['avg_bid']:,.0f} "
        f"avg_ask={r['avg_ask']:,.0f}"
    )


# ---------------------------------------------------------
# Persistent strong pressure
# ---------------------------------------------------------

persistent_buyers = [
    r for r in results
    if r["median"] >= STRONG
    and r["buyer_persistence"] >= 0.70
]

persistent_sellers = [
    r for r in results
    if r["median"] <= -STRONG
    and r["seller_persistence"] >= 0.70
]


print()
print("=" * 125)
print("HIGH-CONFIDENCE PERSISTENT PRESSURE")
print("=" * 125)

print(
    "BUYERS: median >= +0.20 "
    "AND buyer persistence >=70%"
)
print(
    "SELLERS: median <= -0.20 "
    "AND seller persistence >=70%"
)

print()

print(
    "Persistent strong buyers :",
    len(persistent_buyers),
)

for r in sorted(
    persistent_buyers,
    key=lambda x: (
        x["median"],
        x["buyer_persistence"],
    ),
    reverse=True,
):
    print(
        f"  {r['symbol']:<15} "
        f"imbalance={r['median']:+.3f} "
        f"persistence="
        f"{r['buyer_persistence']*100:.1f}%"
    )

print()

print(
    "Persistent strong sellers:",
    len(persistent_sellers),
)

for r in sorted(
    persistent_sellers,
    key=lambda x: (
        x["median"],
        -x["seller_persistence"],
    ),
):
    print(
        f"  {r['symbol']:<15} "
        f"imbalance={r['median']:+.3f} "
        f"persistence="
        f"{r['seller_persistence']*100:.1f}%"
    )


print()
print("=" * 125)
print("INTERPRETATION")
print("=" * 125)

print("""
+0.60 to +1.00 = EXTREME BUYERS
+0.20 to +0.60 = STRONG BUYERS
+0.05 to +0.20 = SLIGHT BUYERS
-0.05 to +0.05 = BALANCED
-0.20 to -0.05 = SLIGHT SELLERS
-0.60 to -0.20 = STRONG SELLERS
-1.00 to -0.60 = EXTREME SELLERS

BUY%  = percentage of samples with imbalance >= +0.20
SELL% = percentage of samples with imbalance <= -0.20

This uses the median five-level imbalance over the final
10-minute window for each stock rather than one isolated tick.
""")

