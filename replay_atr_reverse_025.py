#!/usr/bin/env python3

import json, gzip
from pathlib import Path
from datetime import datetime
from collections import defaultdict

RESET = datetime.fromisoformat("2026-08-26T10:55:09")
END   = datetime.fromisoformat("2026-08-26T15:15:00")

TICKS  = Path("runtime/equity_socket_shadow/ticks_2026-08-26.jsonl.gz")
TRADES = Path("trade_history.jsonl")

THRESHOLD = 0.25

def parse_dt(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except:
        return None

# ----------------------------------------------------------
# Load/group actual post-reset entries
# ----------------------------------------------------------
legs = []

with TRADES.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except:
            continue

        t = parse_dt(x.get("entry_time"))
        if not t or t < RESET or t > END:
            continue

        qd = x.get("entry_quality_detail") or {}
        emaatr = qd.get("ema_distance_atr")
        atr = qd.get("atr")

        if emaatr is None or atr is None:
            continue

        symbol = x.get("symbol")
        side = str(
            x.get("direction") or
            x.get("side") or
            x.get("transaction_type") or ""
        ).upper()

        if side not in ("BUY", "SELL"):
            continue

        entry = x.get("entry")
        qty = x.get("qty") or x.get("quantity")

        try:
            entry = float(entry)
            qty = int(qty)
            atr = float(atr)
            emaatr = float(emaatr)
        except:
            continue

        legs.append({
            "symbol": symbol,
            "time": t,
            "side": side,
            "entry": entry,
            "qty": qty,
            "atr": atr,
            "emaatr": emaatr,
            "actual_pnl": float(
                x.get("net_pnl")
                or x.get("pnl")
                or x.get("profit_loss")
                or 0
            ),
        })

# Group scalp/runner legs belonging to same entry.
groups = {}

for x in legs:
    key = (
        x["symbol"],
        x["time"],
        x["side"],
        x["entry"],
    )

    if key not in groups:
        groups[key] = dict(x)
        groups[key]["qty"] = 0
        groups[key]["actual_pnl"] = 0.0

    groups[key]["qty"] += x["qty"]
    groups[key]["actual_pnl"] += x["actual_pnl"]

trades = sorted(groups.values(), key=lambda x: x["time"])

print("=" * 120)
print("ATR 0.25 REAL-TICK REVERSE REPLAY")
print("=" * 120)
print("Post-reset grouped entries:", len(trades))

# ----------------------------------------------------------
# Load only ticks for required symbols
# ----------------------------------------------------------
wanted = {x["symbol"] for x in trades}
ticks = defaultdict(list)

with gzip.open(TICKS, "rt", errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except:
            continue

        symbol = x.get("symbol")
        if symbol not in wanted:
            continue

        t = parse_dt(x.get("exchange_timestamp"))
        p = x.get("last_price")

        if not t or p is None:
            continue

        if RESET <= t <= END:
            try:
                ticks[symbol].append((t, float(p)))
            except:
                pass

for s in ticks:
    ticks[s].sort()

# ----------------------------------------------------------
# Counterfactual model
#
# <=0.25 ATR:
#   keep actual trade/result.
#
# >0.25 ATR:
#   reverse side.
#
# To avoid pretending the old BUY stop is a valid SELL stop,
# construct symmetric reverse levels from ATR.
#
# Initial reverse stop = 1 ATR
# Target 1 = 1 ATR
# Target 2 = 2 ATR
#
# Position split approximately 50/50.
# ----------------------------------------------------------

def replay_reverse(tr):
    symbol = tr["symbol"]
    original = tr["side"]
    reverse = "SELL" if original == "BUY" else "BUY"

    entry = tr["entry"]
    atr = tr["atr"]
    qty = tr["qty"]

    q1 = qty // 2
    q2 = qty - q1

    if reverse == "BUY":
        stop = entry - atr
        t1 = entry + atr
        t2 = entry + 2 * atr
    else:
        stop = entry + atr
        t1 = entry - atr
        t2 = entry - 2 * atr

    future = [
        (t,p) for t,p in ticks.get(symbol, [])
        if t >= tr["time"]
    ]

    if not future:
        return {
            "side": reverse,
            "result": "NO_TICKS",
            "pnl": 0.0,
            "stop": stop,
            "t1": t1,
            "t2": t2
        }

    t1_hit = False
    pnl = 0.0
    remaining = qty
    last_price = entry
    exit_time = None
    result = "EOD"

    for t,p in future:
        last_price = p

        if reverse == "BUY":
            # Stop first
            if p <= stop:
                pnl += (stop-entry) * remaining
                result = "STOP"
                exit_time = t
                remaining = 0
                break

            if not t1_hit and p >= t1:
                pnl += (t1-entry) * q1
                remaining -= q1
                t1_hit = True
                result = "T1"

            if t1_hit and p >= t2:
                pnl += (t2-entry) * remaining
                result = "T2"
                exit_time = t
                remaining = 0
                break

        else:
            if p >= stop:
                pnl += (entry-stop) * remaining
                result = "STOP"
                exit_time = t
                remaining = 0
                break

            if not t1_hit and p <= t1:
                pnl += (entry-t1) * q1
                remaining -= q1
                t1_hit = True
                result = "T1"

            if t1_hit and p <= t2:
                pnl += (entry-t2) * remaining
                result = "T2"
                exit_time = t
                remaining = 0
                break

    # Square off remaining qty at final recorded tick
    if remaining:
        if reverse == "BUY":
            pnl += (last_price-entry) * remaining
        else:
            pnl += (entry-last_price) * remaining
        exit_time = future[-1][0]
        result += "+EOD"

    return {
        "side": reverse,
        "result": result,
        "pnl": pnl,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "exit_time": exit_time,
    }

# ----------------------------------------------------------
# Results
# ----------------------------------------------------------
baseline = sum(x["actual_pnl"] for x in trades)

blocked_strategy = sum(
    x["actual_pnl"]
    for x in trades
    if x["emaatr"] <= THRESHOLD
)

reverse_total = 0.0
reverse_rows = []

print()
print(
    f"{'TIME':8} {'SYMBOL':12} {'EMAATR':>7} "
    f"{'OLD':>5} {'NEW':>5} {'RESULT':>10} {'PNL':>12}"
)
print("-" * 80)

for tr in trades:

    if tr["emaatr"] <= THRESHOLD:
        pnl = tr["actual_pnl"]
        reverse_total += pnl

        print(
            f"{tr['time'].strftime('%H:%M'):8} "
            f"{tr['symbol']:12} "
            f"{tr['emaatr']:7.3f} "
            f"{tr['side']:>5} "
            f"{tr['side']:>5} "
            f"{'NORMAL':>10} "
            f"{pnl:12.2f}"
        )
        continue

    r = replay_reverse(tr)
    reverse_total += r["pnl"]
    reverse_rows.append((tr, r))

    print(
        f"{tr['time'].strftime('%H:%M'):8} "
        f"{tr['symbol']:12} "
        f"{tr['emaatr']:7.3f} "
        f"{tr['side']:>5} "
        f"{r['side']:>5} "
        f"{r['result']:>10} "
        f"{r['pnl']:12.2f}"
    )

print()
print("=" * 120)
print("STRATEGY COMPARISON")
print("=" * 120)

print(f"A) Actual strategy                       : ₹{baseline:+.2f}")
print(f"B) <=0.25 normal, >0.25 BLOCK           : ₹{blocked_strategy:+.2f}")
print(f"C) <=0.25 normal, >0.25 REVERSE          : ₹{reverse_total:+.2f}")

print()
print("Return on ₹5,000:")
print(f"A) {baseline/5000*100:+.2f}%")
print(f"B) {blocked_strategy/5000*100:+.2f}%")
print(f"C) {reverse_total/5000*100:+.2f}%")

print()
print("=" * 120)
print("REVERSED TRADE DETAILS")
print("=" * 120)

for tr,r in reverse_rows:
    print(
        f"{tr['symbol']:12} "
        f"EMAATR={tr['emaatr']:.3f} "
        f"{tr['side']}->{r['side']} "
        f"entry={tr['entry']:.2f} "
        f"ATR={tr['atr']:.4f} "
        f"SL={r['stop']:.2f} "
        f"T1={r['t1']:.2f} "
        f"T2={r['t2']:.2f} "
        f"result={r['result']} "
        f"pnl=₹{r['pnl']:+.2f}"
    )

print()
print("NOTE: gross counterfactual P&L before estimated brokerage/charges.")
print("Reverse trades use actual recorded tick prices, not sign-flipped P&L.")
