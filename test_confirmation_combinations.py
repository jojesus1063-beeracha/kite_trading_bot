#!/usr/bin/env python3

"""
Replay confirmation combinations from the already-saved 15-trade
microstructure score output.

NO LIVE CHANGES.
NO RAW TICK READING.
NO ORDERS.
"""

import re
from pathlib import Path

SRC = Path(
    "runtime/depth_score_from_saved_analysis_2026-08-27.txt"
)

if not SRC.exists():
    raise SystemExit(f"ABORT: missing {SRC}")

text = SRC.read_text(errors="replace")

# Example expected line:
# 01. TATAPOWER SELL PnL=₹ +144.13 SCORE=2/8 |
# EMA=N DI=Y DEPTH=N PERSIST=N STRENGTH=N
# BOOK=N PRICE=N QUOTE=Y

pat = re.compile(
    r"^\s*\d+\.\s+"
    r"(?P<symbol>\S+)\s+"
    r"(?P<direction>BUY|SELL)\s+"
    r"PnL=₹\s*(?P<pnl>[+-]?\d+(?:\.\d+)?)\s+"
    r"SCORE=\d+/8\s+\|\s+"
    r"EMA=(?P<ema>[YN])\s+"
    r"DI=(?P<di>[YN])\s+"
    r"DEPTH=(?P<depth>[YN])\s+"
    r"PERSIST=(?P<persist>[YN])\s+"
    r"STRENGTH=(?P<strength>[YN])\s+"
    r"BOOK=(?P<book>[YN])\s+"
    r"PRICE=(?P<price>[YN])\s+"
    r"QUOTE=(?P<quote>[YN])",
    re.MULTILINE,
)

trades = []

for m in pat.finditer(text):
    d = m.groupdict()

    trades.append({
        "symbol": d["symbol"],
        "direction": d["direction"],
        "pnl": float(d["pnl"]),
        "EMA": d["ema"] == "Y",
        "DI": d["di"] == "Y",
        "DEPTH": d["depth"] == "Y",
        "PERSIST": d["persist"] == "Y",
        "STRENGTH": d["strength"] == "Y",
        "BOOK": d["book"] == "Y",
        "PRICE": d["price"] == "Y",
        "QUOTE": d["quote"] == "Y",
    })

if len(trades) != 15:
    raise SystemExit(
        f"ABORT: expected 15 trades, parsed {len(trades)}"
    )

rules = {
    "BASELINE": (),
    "PRICE + QUOTE": (
        "PRICE",
        "QUOTE",
    ),
    "DI + QUOTE": (
        "DI",
        "QUOTE",
    ),
    "DI + PRICE": (
        "DI",
        "PRICE",
    ),
    "EMA + PRICE + QUOTE": (
        "EMA",
        "PRICE",
        "QUOTE",
    ),
    "EMA + DI + PRICE + QUOTE": (
        "EMA",
        "DI",
        "PRICE",
        "QUOTE",
    ),

    # Useful additional comparisons
    "DI + PRICE + QUOTE": (
        "DI",
        "PRICE",
        "QUOTE",
    ),
    "DI + STRENGTH + PRICE + QUOTE": (
        "DI",
        "STRENGTH",
        "PRICE",
        "QUOTE",
    ),
    "EMA + DI + STRENGTH + PRICE + QUOTE": (
        "EMA",
        "DI",
        "STRENGTH",
        "PRICE",
        "QUOTE",
    ),
}


def accepted(t, components):
    return all(t[c] for c in components)


def result(components):
    rows = [
        t for t in trades
        if accepted(t, components)
    ]

    wins = sum(t["pnl"] > 0 for t in rows)
    losses = sum(t["pnl"] < 0 for t in rows)
    pnl = sum(t["pnl"] for t in rows)

    winrate = (
        wins / len(rows) * 100
        if rows else 0.0
    )

    return rows, wins, losses, winrate, pnl


print("=" * 125)
print("AUGUST 27 — CONFIRMATION COMBINATION REPLAY")
print("=" * 125)

print(
    f"{'RULE':45}"
    f"{'TRADES':>8}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>15}"
)

print("-" * 125)

for name, components in rules.items():

    rows, wins, losses, wr, pnl = result(components)

    print(
        f"{name:45}"
        f"{len(rows):>8}"
        f"{wins:>6}"
        f"{losses:>6}"
        f"{wr:>9.2f}%"
        f"₹{pnl:>+13.2f}"
    )


print()
print("=" * 125)
print("TRADE-BY-TRADE FOR EACH TEST")
print("=" * 125)

for name, components in rules.items():

    if name == "BASELINE":
        continue

    rows, wins, losses, wr, pnl = result(components)

    print()
    print("-" * 125)
    print(name)
    print("-" * 125)

    if not rows:
        print("NO TRADES")
        continue

    for t in rows:

        flags = " ".join(
            f"{c}=Y"
            for c in components
        )

        result_text = (
            "WIN" if t["pnl"] > 0 else "LOSS"
        )

        print(
            f"{t['symbol']:12} "
            f"{t['direction']:4} "
            f"PnL=₹{t['pnl']:+8.2f} "
            f"{result_text:4} | "
            f"{flags}"
        )

    print(
        f"TOTAL: trades={len(rows)} "
        f"W={wins} "
        f"L={losses} "
        f"win={wr:.2f}% "
        f"net=₹{pnl:+.2f}"
    )


print()
print("=" * 125)
print("FILTER IMPACT VS BASELINE")
print("=" * 125)

baseline_pnl = sum(t["pnl"] for t in trades)

for name, components in rules.items():

    if name == "BASELINE":
        continue

    rows, wins, losses, wr, pnl = result(components)

    rejected = [
        t for t in trades
        if not accepted(t, components)
    ]

    avoided_losses = -sum(
        t["pnl"]
        for t in rejected
        if t["pnl"] < 0
    )

    lost_profit = sum(
        t["pnl"]
        for t in rejected
        if t["pnl"] > 0
    )

    improvement = pnl - baseline_pnl

    print()
    print(name)
    print(
        f"  Accepted        : {len(rows)}"
    )
    print(
        f"  Net PnL         : ₹{pnl:+.2f}"
    )
    print(
        f"  Loss avoided    : ₹{avoided_losses:.2f}"
    )
    print(
        f"  Profit rejected : ₹{lost_profit:.2f}"
    )
    print(
        f"  Vs baseline     : ₹{improvement:+.2f}"
    )


print()
print("=" * 125)
print("DONE — ANALYSIS ONLY; BOT UNCHANGED")
print("=" * 125)
