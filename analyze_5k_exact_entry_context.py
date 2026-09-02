#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

FILE = Path("trade_history.jsonl")
RESET = datetime.fromisoformat("2026-08-26T10:55:09")

def dt(v):
    if not v:
        return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x.replace(tzinfo=None)
    except:
        return None

rows = []
with FILE.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except:
            continue

        if x.get("date") != "2026-08-26":
            continue

        et = dt(x.get("entry_time"))
        if not et or et < RESET:
            continue

        rows.append(x)

# ---------------------------------------------------------
# Group exit legs belonging to ONE actual entry
# ---------------------------------------------------------

groups = defaultdict(list)

for x in rows:
    key = (
        x.get("symbol"),
        x.get("direction"),
        x.get("entry"),
        x.get("entry_time"),
        x.get("signal_id"),
    )
    groups[key].append(x)

trades = []

for key, legs in groups.items():
    x = legs[0]

    q = x.get("entry_quality_detail") or {}
    c = x.get("entry_context_detail") or {}

    pnl = sum(float(z.get("pnl") or 0) for z in legs)

    submitted = dt(x.get("order_submitted_at"))
    entry_time = dt(x.get("entry_time"))

    submit_delay = None
    if submitted and entry_time:
        submit_delay = (submitted - entry_time).total_seconds()

    trades.append({
        "symbol": x.get("symbol"),
        "direction": x.get("direction"),
        "entry": x.get("entry"),
        "entry_time": entry_time,
        "pnl": pnl,
        "legs": len(legs),

        "ema": q.get("ema_distance_atr"),
        "vwap": q.get("vwap_distance_atr"),
        "body": q.get("signal_body_atr"),

        "adx": x.get("adx_current"),
        "adx_delta": x.get("adx_delta"),
        "adx_state": x.get("adx_state"),

        "rank": x.get("candidate_rank"),
        "candidates": x.get("candidate_count"),

        "context": x.get("entry_context_score"),

        "market_structure": bool(c.get("market_structure")),
        "breakout": bool(c.get("breakout")),
        "pullback": bool(c.get("pullback")),
        "bos": bool(c.get("bos")),
        "choch": bool(c.get("choch")),

        "submit_delay": submit_delay,
        "recorded_delay": x.get("entry_delay_seconds"),
    })

trades.sort(key=lambda z: z["entry_time"])

baseline = sum(t["pnl"] for t in trades)

print("=" * 125)
print("₹5,000 EXACT ENTRY-CONTEXT ANALYSIS")
print("=" * 125)
print(f"Reset time          : {RESET}")
print(f"Exit legs           : {len(rows)}")
print(f"Actual grouped trades: {len(trades)}")
print(f"Baseline net P&L    : ₹{baseline:.2f}")
print()

print(
    f"{'TIME':8} {'SYMBOL':12} {'DIR':5} {'PNL':>9} "
    f"{'EMAATR':>7} {'VWAPATR':>8} {'BODY':>7} "
    f"{'ADX':>6} {'STATE':>8} {'RANK':>6} "
    f"{'CTX':>5} {'DELAY':>7}"
)
print("-" * 125)

for t in trades:
    print(
        f"{t['entry_time'].strftime('%H:%M'):8} "
        f"{str(t['symbol']):12} "
        f"{str(t['direction']):5} "
        f"{t['pnl']:9.2f} "
        f"{float(t['ema'] or 0):7.3f} "
        f"{float(t['vwap'] or 0):8.3f} "
        f"{float(t['body'] or 0):7.3f} "
        f"{float(t['adx'] or 0):6.1f} "
        f"{str(t['adx_state']):8} "
        f"{str(t['rank']):>6} "
        f"{float(t['context'] or 0):5.0f} "
        f"{float(t['recorded_delay'] or 0):7.0f}"
    )

# ---------------------------------------------------------
# Winner / loser averages
# ---------------------------------------------------------

wins = [t for t in trades if t["pnl"] > 0]
losses = [t for t in trades if t["pnl"] <= 0]

print()
print("=" * 125)
print("WINNER vs LOSER ENTRY SNAPSHOT")
print("=" * 125)

metrics = [
    ("ema", "EMA distance ATR"),
    ("vwap", "VWAP distance ATR"),
    ("body", "Signal body ATR"),
    ("adx", "ADX"),
    ("adx_delta", "ADX delta"),
    ("rank", "Candidate rank"),
    ("context", "Context score"),
    ("recorded_delay", "Entry delay sec"),
]

def avg(arr, key):
    vals = []
    for t in arr:
        try:
            if t[key] is not None:
                vals.append(float(t[key]))
        except:
            pass
    return sum(vals)/len(vals) if vals else None

for key, label in metrics:
    wa = avg(wins, key)
    la = avg(losses, key)

    print(
        f"{label:25} "
        f"WIN={wa if wa is not None else 'NA':>10} "
        f"LOSS={la if la is not None else 'NA':>10}"
    )

# ---------------------------------------------------------
# Generic counterfactual evaluator
# ---------------------------------------------------------

print()
print("=" * 125)
print("COUNTERFACTUAL HARD-GATE REPLAY")
print("=" * 125)

def test(name, predicate):
    kept = []

    for t in trades:
        try:
            if predicate(t):
                kept.append(t)
        except:
            pass

    pnl = sum(t["pnl"] for t in kept)
    w = sum(t["pnl"] > 0 for t in kept)
    l = sum(t["pnl"] <= 0 for t in kept)

    print(
        f"{name:38} "
        f"N={len(kept):2} "
        f"W={w:2} L={l:2} "
        f"PNL=₹{pnl:+9.2f} "
        f"DELTA=₹{pnl-baseline:+9.2f}"
    )

# EMA-distance sweep
for v in [0.10,0.20,0.25,0.30,0.40,0.50,0.75,1.00,1.25,1.50,2.00]:
    test(
        f"EMA distance <= {v:.2f} ATR",
        lambda t, v=v: t["ema"] is not None and float(t["ema"]) <= v
    )

print()

# VWAP-distance sweep
for v in [0.5,1.0,1.5,2.0,3.0,5.0,7.5,10.0,15.0]:
    test(
        f"VWAP distance <= {v:.1f} ATR",
        lambda t, v=v: t["vwap"] is not None and float(t["vwap"]) <= v
    )

print()

# Signal body
for v in [0.10,0.20,0.30,0.50,0.75,1.00]:
    test(
        f"Signal body >= {v:.2f} ATR",
        lambda t, v=v: t["body"] is not None and float(t["body"]) >= v
    )

print()

# ADX
for v in [10,15,20,25,30,35,40]:
    test(
        f"ADX >= {v}",
        lambda t, v=v: t["adx"] is not None and float(t["adx"]) >= v
    )

test(
    "ADX RISING",
    lambda t: str(t["adx_state"]).upper() == "RISING"
)

test(
    "ADX FALLING",
    lambda t: str(t["adx_state"]).upper() == "FALLING"
)

print()

# Candidate ranking
for v in [5,10,15,20,30,50,75,100]:
    test(
        f"Candidate rank <= {v}",
        lambda t, v=v: t["rank"] is not None and int(t["rank"]) <= v
    )

print()

# Delay
for v in [120,150,180,210,240,300,360]:
    test(
        f"Entry delay <= {v}s",
        lambda t, v=v:
            t["recorded_delay"] is not None
            and float(t["recorded_delay"]) <= v
    )

print()

# Context features
test("Pullback", lambda t: t["pullback"])
test("BOS", lambda t: t["bos"])
test("CHOCH", lambda t: t["choch"])
test("Breakout", lambda t: t["breakout"])
test("Market structure", lambda t: t["market_structure"])

print()
print("=" * 125)
print("IMPORTANT")
print("=" * 125)
print("These calculations use the context stored on the actual trade record.")
print("Scalp/runner exit legs are grouped into one strategy entry.")
print("Only entries at/after the ₹5,000 reset are included.")
print("A profitable threshold from one day is NOT sufficient evidence")
print("to turn that threshold into a production hard gate.")
print("=" * 125)
