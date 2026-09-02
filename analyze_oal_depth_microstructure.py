#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from statistics import mean

TICK_FILE = Path(
    "runtime/equity_socket_shadow/"
    "ticks_2026-08-27_recovered.jsonl"
)

SYMBOL = "OAL"

# Actual OAL signal:
# signal candle close = 12:00:00
# scan = 12:00:12
# order submitted = 12:00:23.552517
DECISION = datetime.fromisoformat(
    "2026-08-27T12:00:23.552517+05:30"
)

WINDOW_SECONDS = 30

IST = timezone(timedelta(hours=5, minutes=30))


def parse_dt(v):
    if not v:
        return None

    try:
        dt = datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)

        return dt
    except Exception:
        return None


def qty(row):
    try:
        return float(row.get("quantity") or 0)
    except Exception:
        return 0.0


def price(row):
    try:
        return float(row.get("price") or 0)
    except Exception:
        return 0.0


def orders(row):
    try:
        return int(row.get("orders") or 0)
    except Exception:
        return 0


rows = []

start = DECISION - timedelta(seconds=WINDOW_SECONDS)

with TICK_FILE.open("r", errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        if str(x.get("symbol") or "").upper() != SYMBOL:
            continue

        ts = (
            x.get("exchange_timestamp")
            or x.get("received_at")
            or x.get("timestamp")
        )

        dt = parse_dt(ts)

        if dt is None:
            continue

        if dt < start or dt > DECISION:
            continue

        depth = x.get("depth") or {}

        buys = depth.get("buy") or []
        sells = depth.get("sell") or []

        if not buys or not sells:
            continue

        buy_qty = sum(qty(r) for r in buys[:5])
        sell_qty = sum(qty(r) for r in sells[:5])

        total = buy_qty + sell_qty

        imbalance = (
            (buy_qty - sell_qty) / total
            if total else None
        )

        best_bid = price(buys[0])
        best_ask = price(sells[0])

        largest_bid = max(
            buys[:5],
            key=qty
        )

        largest_ask = max(
            sells[:5],
            key=qty
        )

        ltp = x.get("last_price")

        rows.append({
            "dt": dt,
            "ltp": float(ltp) if ltp is not None else None,

            "best_bid": best_bid,
            "best_bid_qty": qty(buys[0]),
            "best_bid_orders": orders(buys[0]),

            "best_ask": best_ask,
            "best_ask_qty": qty(sells[0]),
            "best_ask_orders": orders(sells[0]),

            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "imb": imbalance,

            "largest_bid_price": price(largest_bid),
            "largest_bid_qty": qty(largest_bid),
            "largest_bid_orders": orders(largest_bid),

            "largest_ask_price": price(largest_ask),
            "largest_ask_qty": qty(largest_ask),
            "largest_ask_orders": orders(largest_ask),

            "aggressor": x.get("aggressor"),
            "last_quantity": x.get("last_quantity"),
            "classified_volume": x.get("classified_volume"),
            "depth_change": x.get("depth_change_inference"),
        })


print("=" * 150)
print("OAL — 30 SECOND PRE-ENTRY DEPTH MICROSTRUCTURE")
print("=" * 150)

print("Decision time :", DECISION.isoformat())
print("Window start  :", start.isoformat())
print("Snapshots     :", len(rows))

if not rows:
    raise SystemExit("NO OAL DEPTH SNAPSHOTS FOUND")


print()
print("=" * 150)
print("EVERY DEPTH SNAPSHOT")
print("=" * 150)

for r in rows:

    ltp = (
        f"{r['ltp']:.2f}"
        if r["ltp"] is not None
        else "NA"
    )

    imb = (
        f"{r['imb']:+.3f}"
        if r["imb"] is not None
        else "NA"
    )

    print(
        f"{r['dt'].strftime('%H:%M:%S.%f')[:-3]} "
        f"LTP={ltp:<7} "
        f"BID={r['best_bid']:.2f}"
        f"x{r['best_bid_qty']:.0f} "
        f"ASK={r['best_ask']:.2f}"
        f"x{r['best_ask_qty']:.0f} "
        f"5B={r['buy_qty']:.0f} "
        f"5S={r['sell_qty']:.0f} "
        f"IMB={imb} "
        f"BIG_BID={r['largest_bid_price']:.2f}"
        f"x{r['largest_bid_qty']:.0f} "
        f"BIG_ASK={r['largest_ask_price']:.2f}"
        f"x{r['largest_ask_qty']:.0f} "
        f"AGG={r['aggressor']}"
    )


first = rows[0]
last = rows[-1]

valid_imb = [
    r["imb"] for r in rows
    if r["imb"] is not None
]

bullish = [
    x for x in valid_imb
    if x >= 0.20
]

bearish = [
    x for x in valid_imb
    if x <= -0.20
]


print()
print("=" * 150)
print("BOOK EVOLUTION")
print("=" * 150)

print(
    f"First LTP       : {first['ltp']}"
)
print(
    f"Last LTP        : {last['ltp']}"
)

if first["ltp"] is not None and last["ltp"] is not None:
    print(
        f"LTP change      : "
        f"{last['ltp'] - first['ltp']:+.2f}"
    )

print(
    f"Best bid        : "
    f"{first['best_bid']:.2f} -> "
    f"{last['best_bid']:.2f}"
)

print(
    f"Best ask        : "
    f"{first['best_ask']:.2f} -> "
    f"{last['best_ask']:.2f}"
)

print(
    f"5-level BUY qty : "
    f"{first['buy_qty']:.0f} -> "
    f"{last['buy_qty']:.0f} "
    f"({last['buy_qty']-first['buy_qty']:+.0f})"
)

print(
    f"5-level SELL qty: "
    f"{first['sell_qty']:.0f} -> "
    f"{last['sell_qty']:.0f} "
    f"({last['sell_qty']-first['sell_qty']:+.0f})"
)

print(
    f"Largest bid     : "
    f"₹{last['largest_bid_price']:.2f} "
    f"qty={last['largest_bid_qty']:.0f} "
    f"orders={last['largest_bid_orders']}"
)

print(
    f"Largest ask     : "
    f"₹{last['largest_ask_price']:.2f} "
    f"qty={last['largest_ask_qty']:.0f} "
    f"orders={last['largest_ask_orders']}"
)

print(
    f"Average imbalance: "
    f"{mean(valid_imb):+.3f}"
)

print(
    f"Bullish >=+0.20 : "
    f"{len(bullish)}/{len(valid_imb)} "
    f"({100*len(bullish)/len(valid_imb):.1f}%)"
)

print(
    f"Bearish <=-0.20 : "
    f"{len(bearish)}/{len(valid_imb)} "
    f"({100*len(bearish)/len(valid_imb):.1f}%)"
)


print()
print("=" * 150)
print("DISTANCE OF WALLS FROM LTP — FINAL SNAPSHOT")
print("=" * 150)

if last["ltp"] is not None:

    bid_distance = (
        last["ltp"]
        - last["largest_bid_price"]
    )

    ask_distance = (
        last["largest_ask_price"]
        - last["ltp"]
    )

    print(
        f"LTP                 = ₹{last['ltp']:.2f}"
    )

    print(
        f"Largest BUY wall    = "
        f"₹{last['largest_bid_price']:.2f} "
        f"qty={last['largest_bid_qty']:.0f}"
    )

    print(
        f"BUY wall below LTP  = "
        f"₹{bid_distance:.2f} "
        f"({bid_distance/last['ltp']*100:.3f}%)"
    )

    print(
        f"Largest SELL wall   = "
        f"₹{last['largest_ask_price']:.2f} "
        f"qty={last['largest_ask_qty']:.0f}"
    )

    print(
        f"SELL wall above LTP = "
        f"₹{ask_distance:.2f} "
        f"({ask_distance/last['ltp']*100:.3f}%)"
    )


print()
print("=" * 150)
print("AGGRESSOR FLOW")
print("=" * 150)

agg = {}

for r in rows:
    a = str(r["aggressor"] or "UNKNOWN").upper()

    try:
        q = float(r["last_quantity"] or 0)
    except Exception:
        q = 0

    if a not in agg:
        agg[a] = {
            "ticks": 0,
            "qty": 0,
        }

    agg[a]["ticks"] += 1
    agg[a]["qty"] += q

for side, data in sorted(agg.items()):
    print(
        f"{side:<12} "
        f"ticks={data['ticks']:<6} "
        f"last_qty_sum={data['qty']:.0f}"
    )


print()
print("=" * 150)
print("INTERPRETATION INPUTS")
print("=" * 150)

print(
    "Use together: wall location + wall movement + "
    "imbalance persistence + aggressor flow + LTP response."
)

print(
    "Do NOT conclude BUY merely because total bid "
    "quantity exceeds total ask quantity."
)
