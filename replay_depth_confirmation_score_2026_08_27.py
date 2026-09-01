#!/usr/bin/env python3

"""
Aug 27 depth/DI/price confirmation scoring replay.

ANALYSIS ONLY.
NO CONFIG CHANGES.
NO ORDERS.

Scores each historical entry using only information that was
available at/before its original decision timestamp.
"""

import runpy

SOURCE = "analyze_depth_behavior_all_trades_2026_08_27.py"

print("=" * 150)
print("LOADING EXISTING AUG-27 MICROSTRUCTURE REPLAY")
print("=" * 150)

ns = runpy.run_path(SOURCE)

# Find the trade collection produced by the existing script.
trades = None

for name in ("results", "trades", "entries", "analyses"):
    value = ns.get(name)
    if isinstance(value, list) and value:
        if isinstance(value[0], dict):
            trades = value
            print(f"Using collection: {name} ({len(value)} rows)")
            break

if trades is None:
    candidates = []
    for name, value in ns.items():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
        ):
            candidates.append((name, value))

    print("\nAvailable list-of-dict objects:")
    for name, value in candidates:
        print(f"  {name}: {len(value)}")

    raise SystemExit(
        "\nCould not automatically identify result collection. "
        "Paste the 'Available list-of-dict objects' output."
    )


def first(d, *names, default=None):
    for name in names:
        if name in d and d[name] is not None:
            return d[name]
    return default


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def boolean(v):
    if isinstance(v, bool):
        return v
    return None


def calculate_score(t):
    direction = str(
        first(t, "direction", "side", "dir", default="")
    ).upper()

    pdi = num(first(t, "plus_di", "pdi"))
    mdi = num(first(t, "minus_di", "mdi"))

    avg_imb = num(
        first(
            t,
            "avg_imbalance",
            "average_imbalance",
            "imbalance_avg",
        )
    )

    directional_change = num(
        first(
            t,
            "directional_change",
            "directional_imb_change",
            "directional_imbalance_change",
        )
    )

    bid_change = num(
        first(
            t,
            "bid_qty_change_pct",
            "bid_qty_change",
            "bid_change",
        )
    )

    ask_change = num(
        first(
            t,
            "ask_qty_change_pct",
            "ask_qty_change",
            "ask_change",
        )
    )

    ltp_move = num(
        first(
            t,
            "ltp_move",
            "price_move",
            "ltp_change",
        )
    )

    quote_confirm = boolean(
        first(
            t,
            "quote_confirm",
            "quote_confirmation",
        )
    )

    components = {}
    score = 0

    # ---------------------------------------------------------
    # 1. DI agreement
    # ---------------------------------------------------------
    if pdi is not None and mdi is not None:
        di_ok = (
            (direction == "BUY" and pdi > mdi)
            or
            (direction == "SELL" and mdi > pdi)
        )
    else:
        di_ok = boolean(first(t, "di_pass"))

    components["DI"] = bool(di_ok)

    if di_ok:
        score += 1

    # ---------------------------------------------------------
    # 2. Static depth direction
    # ---------------------------------------------------------
    if avg_imb is None:
        raw_depth = False
    elif direction == "BUY":
        raw_depth = avg_imb >= 0.20
    elif direction == "SELL":
        raw_depth = avg_imb <= -0.20
    else:
        raw_depth = False

    components["RAW_DEPTH"] = raw_depth

    if raw_depth:
        score += 1

    # ---------------------------------------------------------
    # 3. Depth strengthening
    #
    # directional_change has already been normalized:
    # positive = improving in trade direction.
    # ---------------------------------------------------------
    depth_strengthening = (
        directional_change is not None
        and directional_change > 0
    )

    components["DEPTH_STRENGTH"] = depth_strengthening

    if depth_strengthening:
        score += 1

    # ---------------------------------------------------------
    # 4/5. Bid/ask liquidity evolution
    # ---------------------------------------------------------
    if direction == "BUY":
        supportive_liquidity = (
            bid_change is not None and bid_change > 0
        )
        opposing_liquidity = (
            ask_change is not None and ask_change < 0
        )

    elif direction == "SELL":
        supportive_liquidity = (
            ask_change is not None and ask_change > 0
        )
        opposing_liquidity = (
            bid_change is not None and bid_change < 0
        )

    else:
        supportive_liquidity = False
        opposing_liquidity = False

    components["SUPPORTIVE_LIQ"] = supportive_liquidity
    components["OPPOSING_LIQ_WEAKENS"] = opposing_liquidity

    if supportive_liquidity:
        score += 1

    if opposing_liquidity:
        score += 1

    # ---------------------------------------------------------
    # 6/7. Actual LTP response — DOUBLE WEIGHT
    # ---------------------------------------------------------
    if ltp_move is None:
        price_confirm = False
    elif direction == "BUY":
        price_confirm = ltp_move > 0
    elif direction == "SELL":
        price_confirm = ltp_move < 0
    else:
        price_confirm = False

    components["PRICE"] = price_confirm

    if price_confirm:
        score += 2

    # ---------------------------------------------------------
    # 8. Quote movement
    # ---------------------------------------------------------
    quote_ok = bool(quote_confirm)

    components["QUOTE"] = quote_ok

    if quote_ok:
        score += 1

    return score, components


def pnl_of(t):
    return num(
        first(
            t,
            "pnl",
            "net_pnl",
            "total_pnl",
            "actual_pnl",
            default=0,
        )
    ) or 0.0


def symbol_of(t):
    return str(
        first(t, "symbol", "tradingsymbol", default="?")
    )


def direction_of(t):
    return str(
        first(t, "direction", "side", "dir", default="?")
    ).upper()


scored = []

for t in trades:
    score, components = calculate_score(t)

    row = {
        "symbol": symbol_of(t),
        "direction": direction_of(t),
        "pnl": pnl_of(t),
        "score": score,
        "components": components,
    }

    scored.append(row)


print()
print("=" * 150)
print("TRADE-BY-TRADE CONFIRMATION SCORE")
print("=" * 150)

for i, r in enumerate(scored, 1):
    c = r["components"]

    flags = (
        f"DI={'Y' if c['DI'] else 'N'} "
        f"RAW={'Y' if c['RAW_DEPTH'] else 'N'} "
        f"dDEPTH={'Y' if c['DEPTH_STRENGTH'] else 'N'} "
        f"SUP={'Y' if c['SUPPORTIVE_LIQ'] else 'N'} "
        f"OPP={'Y' if c['OPPOSING_LIQ_WEAKENS'] else 'N'} "
        f"PRICE={'Y' if c['PRICE'] else 'N'} "
        f"QUOTE={'Y' if c['QUOTE'] else 'N'}"
    )

    print(
        f"{i:02d}. "
        f"{r['symbol']:<12} "
        f"{r['direction']:<4} "
        f"PnL=₹{r['pnl']:+8.2f} "
        f"SCORE={r['score']}/8  "
        f"{flags}"
    )


baseline_pnl = sum(r["pnl"] for r in scored)
baseline_losses = sum(-r["pnl"] for r in scored if r["pnl"] < 0)
baseline_profits = sum(r["pnl"] for r in scored if r["pnl"] > 0)

print()
print("=" * 150)
print("THRESHOLD REPLAY")
print("=" * 150)

print(
    f"{'RULE':<15}"
    f"{'TRADES':>8}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>14}"
    f"{'LOSS AVOID':>15}"
    f"{'PROFIT LOST':>15}"
)

print("-" * 150)

for threshold in range(1, 9):
    accepted = [
        r for r in scored
        if r["score"] >= threshold
    ]

    rejected = [
        r for r in scored
        if r["score"] < threshold
    ]

    wins = sum(r["pnl"] > 0 for r in accepted)
    losses = sum(r["pnl"] < 0 for r in accepted)

    net = sum(r["pnl"] for r in accepted)

    win_pct = (
        wins / len(accepted) * 100
        if accepted
        else 0.0
    )

    loss_avoided = sum(
        -r["pnl"]
        for r in rejected
        if r["pnl"] < 0
    )

    profit_lost = sum(
        r["pnl"]
        for r in rejected
        if r["pnl"] > 0
    )

    print(
        f"SCORE >= {threshold:<5}"
        f"{len(accepted):>8}"
        f"{wins:>6}"
        f"{losses:>6}"
        f"{win_pct:>9.2f}%"
        f"₹{net:>12.2f}"
        f"₹{loss_avoided:>13.2f}"
        f"₹{profit_lost:>13.2f}"
    )


print()
print("=" * 150)
print("SCORE DISTRIBUTION — WINNERS VS LOSERS")
print("=" * 150)

winners = [r for r in scored if r["pnl"] > 0]
losers = [r for r in scored if r["pnl"] < 0]

if winners:
    print(
        "WINNERS:",
        "count=", len(winners),
        "avg_score=",
        round(
            sum(r["score"] for r in winners)
            / len(winners),
            2,
        ),
        "scores=",
        [r["score"] for r in winners],
    )

if losers:
    print(
        "LOSERS :",
        "count=", len(losers),
        "avg_score=",
        round(
            sum(r["score"] for r in losers)
            / len(losers),
            2,
        ),
        "scores=",
        [r["score"] for r in losers],
    )


print()
print("=" * 150)
print("DETAIL BY SCORE")
print("=" * 150)

for score in range(8, -1, -1):
    rows = [r for r in scored if r["score"] == score]

    if not rows:
        continue

    print(f"\nSCORE {score}/8")

    for r in rows:
        result = "WIN" if r["pnl"] > 0 else "LOSS"

        print(
            f"  {r['symbol']:<12} "
            f"{r['direction']:<4} "
            f"{result:<4} "
            f"₹{r['pnl']:+.2f}"
        )


print()
print("=" * 150)
print("BASELINE")
print("=" * 150)

print(f"Trades        : {len(scored)}")
print(f"Net PnL       : ₹{baseline_pnl:+.2f}")
print(f"Gross losses  : ₹{baseline_losses:.2f}")
print(f"Gross profits : ₹{baseline_profits:.2f}")

print()
print(
    "ANALYSIS COMPLETE — "
    "NO BOT CONFIGURATION CHANGED"
)
