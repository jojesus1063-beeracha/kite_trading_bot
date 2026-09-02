#!/usr/bin/env python3

import re
from pathlib import Path

SRC = Path(
    "runtime/depth_behavior_all_trades_2026-08-27.txt"
)

if not SRC.exists():
    raise SystemExit(
        f"ABORT: saved analysis not found: {SRC}"
    )

text = SRC.read_text(errors="replace")

print("=" * 120)
print("LIGHTWEIGHT MICROSTRUCTURE SCORE")
print("=" * 120)
print("SOURCE =", SRC)
print("SIZE   =", len(text), "characters")
print()


# ------------------------------------------------------------
# Locate individual trade sections
# ------------------------------------------------------------

header_re = re.compile(
    r"(?m)^(\d{2})\.\s+"
    r"(?:(\d{2}:\d{2}:\d{2})\s+)?"
    r"([A-Z0-9_-]+)\s+"
    r"(BUY|SELL)\b.*?"
    r"PnL=₹\s*([+-]?\d+(?:\.\d+)?)"
)

matches = list(header_re.finditer(text))

print("TRADE HEADERS FOUND =", len(matches))

if not matches:
    print()
    print("Could not recognise trade blocks.")
    print("Showing candidate PnL lines:")
    print("-" * 120)

    for line in text.splitlines():
        if "PnL=" in line:
            print(line[:250])

    raise SystemExit(
        "ABORT: parser needs adjustment to actual saved format"
    )


def bool_value(body, names):
    for name in names:
        m = re.search(
            rf"{name}\s*=\s*(True|False)",
            body,
            re.I
        )
        if m:
            return m.group(1).lower() == "true"

    return None


def number(body, patterns):
    for pattern in patterns:
        m = re.search(pattern, body, re.I | re.S)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass

    return None


rows = []

for i, m in enumerate(matches):

    end = (
        matches[i + 1].start()
        if i + 1 < len(matches)
        else len(text)
    )

    body = text[m.start():end]

    symbol = m.group(3)
    direction = m.group(4)
    pnl = float(m.group(5))

    avg_imb = number(
        body,
        [
            r"IMB\s+avg\s*=\s*([+-]?\d+(?:\.\d+)?)",
            r"AVG(?:ERAGE)?\s+IMBALANCE\s*[:=]\s*([+-]?\d+(?:\.\d+)?)",
            r"avg_imbalance['\"]?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)",
        ]
    )

    directional_change = number(
        body,
        [
            r"directional_change\s*=\s*([+-]?\d+(?:\.\d+)?)",
            r"DIRECTIONAL\s+IMB(?:ALANCE)?\s+CHANGE\s*[:=]\s*([+-]?\d+(?:\.\d+)?)",
            r"directional_imb_change['\"]?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)",
        ]
    )

    persistence = number(
        body,
        [
            r"persistence\s*=\s*([0-9.]+)\s*%",
            r"PERSISTENCE\s*[:=]\s*([0-9.]+)\s*%",
        ]
    )

    # If stored as decimal rather than percentage.
    if persistence is None:
        p = number(
            body,
            [
                r"persistence['\"]?\s*[:=]\s*([0-9.]+)"
            ]
        )

        if p is not None:
            persistence = (
                p * 100.0 if p <= 1.0 else p
            )

    bid_change = number(
        body,
        [
            r"BID(?:_|\s+)QTY.*?\(([+-]?[0-9.]+)%",
            r"BID\s+QTY\s+CHANGE\s*[:=]\s*([+-]?[0-9.]+)%",
            r"bid_qty_change_pct['\"]?\s*[:=]\s*([+-]?[0-9.]+)",
        ]
    )

    ask_change = number(
        body,
        [
            r"ASK(?:_|\s+)QTY.*?\(([+-]?[0-9.]+)%",
            r"ASK\s+QTY\s+CHANGE\s*[:=]\s*([+-]?[0-9.]+)%",
            r"ask_qty_change_pct['\"]?\s*[:=]\s*([+-]?[0-9.]+)",
        ]
    )

    ema_pass = bool_value(
        body,
        [
            "EMA_PASS",
            "EMA_ATR_PASS",
        ]
    )

    di_pass = bool_value(
        body,
        [
            "DI_PASS",
            "DI_AGREE",
        ]
    )

    persistence_pass = bool_value(
        body,
        ["PERSISTENCE_PASS"]
    )

    price_confirm = bool_value(
        body,
        ["PRICE_CONFIRM"]
    )

    quote_confirm = bool_value(
        body,
        ["QUOTE_CONFIRM"]
    )

    # --------------------------------------------------------
    # Derive score components
    # --------------------------------------------------------

    # EMA
    ema = ema_pass is True

    # DI
    di = di_pass is True

    # Raw depth direction
    if avg_imb is None:
        depth = False

    elif direction == "BUY":
        depth = avg_imb >= 0.20

    else:
        depth = avg_imb <= -0.20

    # Persistence
    if persistence_pass is not None:
        persist = persistence_pass
    else:
        persist = (
            persistence is not None
            and persistence >= 70.0
        )

    # Is directional imbalance becoming stronger?
    strength = (
        directional_change is not None
        and directional_change > 0
    )

    # Book evolution:
    #
    # BUY  -> bids should strengthen relative to asks
    # SELL -> asks should strengthen relative to bids
    #
    if bid_change is None or ask_change is None:
        book = False

    elif direction == "BUY":
        book = bid_change > ask_change

    else:
        book = ask_change > bid_change

    price = price_confirm is True
    quote = quote_confirm is True

    parts = {
        "EMA": ema,
        "DI": di,
        "DEPTH": depth,
        "PERSIST": persist,
        "STRENGTH": strength,
        "BOOK": book,
        "PRICE": price,
        "QUOTE": quote,
    }

    score = sum(parts.values())

    rows.append({
        "symbol": symbol,
        "direction": direction,
        "pnl": pnl,
        "score": score,
        "parts": parts,
        "avg_imb": avg_imb,
        "persistence": persistence,
        "directional_change": directional_change,
        "bid_change": bid_change,
        "ask_change": ask_change,
    })


print()
print("=" * 150)
print("TRADE-BY-TRADE — CORRECTED 8-POINT SCORE")
print("=" * 150)

for i, r in enumerate(rows, 1):

    flags = " ".join(
        f"{k}={'Y' if v else 'N'}"
        for k, v in r["parts"].items()
    )

    print(
        f"{i:02d}. "
        f"{r['symbol']:<12} "
        f"{r['direction']:<4} "
        f"PnL=₹{r['pnl']:+9.2f} "
        f"SCORE={r['score']}/8 "
        f"| {flags}"
    )


# ------------------------------------------------------------
# Threshold sweep
# ------------------------------------------------------------

print()
print("=" * 150)
print("SCORE THRESHOLD SWEEP")
print("=" * 150)

print(
    f"{'RULE':<12}"
    f"{'TRADES':>8}"
    f"{'WINS':>8}"
    f"{'LOSSES':>8}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>15}"
    f"{'LOSS AVOIDED':>17}"
    f"{'PROFIT LOST':>17}"
)

print("-" * 150)

for threshold in range(0, 9):

    accepted = [
        r for r in rows
        if r["score"] >= threshold
    ]

    rejected = [
        r for r in rows
        if r["score"] < threshold
    ]

    wins = sum(
        r["pnl"] > 0
        for r in accepted
    )

    losses = sum(
        r["pnl"] < 0
        for r in accepted
    )

    net = sum(
        r["pnl"]
        for r in accepted
    )

    win_rate = (
        100.0 * wins / len(accepted)
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
        f"SCORE>={threshold:<5}"
        f"{len(accepted):>8}"
        f"{wins:>8}"
        f"{losses:>8}"
        f"{win_rate:>9.2f}%"
        f"₹{net:>+13.2f}"
        f"₹{loss_avoided:>15.2f}"
        f"₹{profit_lost:>15.2f}"
    )


# ------------------------------------------------------------
# Winner vs loser comparison
# ------------------------------------------------------------

winners = [
    r for r in rows
    if r["pnl"] > 0
]

losers = [
    r for r in rows
    if r["pnl"] < 0
]

print()
print("=" * 150)
print("WINNERS VS LOSERS")
print("=" * 150)

if winners:
    print(
        "WINNERS:",
        len(winners),
        "| average score =",
        round(
            sum(r["score"] for r in winners)
            / len(winners),
            2
        ),
        "| scores =",
        [r["score"] for r in winners],
    )

if losers:
    print(
        "LOSERS :",
        len(losers),
        "| average score =",
        round(
            sum(r["score"] for r in losers)
            / len(losers),
            2
        ),
        "| scores =",
        [r["score"] for r in losers],
    )


print()
print("=" * 150)
print("INDIVIDUAL COMPONENT PERFORMANCE")
print("=" * 150)

for component in [
    "EMA",
    "DI",
    "DEPTH",
    "PERSIST",
    "STRENGTH",
    "BOOK",
    "PRICE",
    "QUOTE",
]:
    accepted = [
        r for r in rows
        if r["parts"][component]
    ]

    if not accepted:
        print(
            f"{component:<10}: no accepted trades"
        )
        continue

    w = sum(
        r["pnl"] > 0
        for r in accepted
    )

    l = sum(
        r["pnl"] < 0
        for r in accepted
    )

    pnl = sum(
        r["pnl"]
        for r in accepted
    )

    wr = (
        w / len(accepted) * 100.0
    )

    print(
        f"{component:<10}: "
        f"trades={len(accepted):2d} "
        f"W={w:2d} "
        f"L={l:2d} "
        f"win={wr:6.2f}% "
        f"net=₹{pnl:+.2f}"
    )


print()
print("=" * 150)
print("DONE — SAVED REPORT ONLY; RAW TICKS NOT READ")
print("=" * 150)
