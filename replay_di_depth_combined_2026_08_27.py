#!/usr/bin/env python3

"""
August 27 combined-filter replay.

Goal:
Compare today's actual/candidate trades under:

BASELINE:
    Existing accepted entry filters.

TEST:
    Existing accepted entry filters
    + EMA9 distance <= 0.25 ATR
    + DI must agree with trade direction
    + market depth must agree with trade direction.

IMPORTANT:
- Do NOT change bot configuration.
- Do NOT place orders.
- PAPER / offline analysis only.
- Use the exact historical state available at each signal/entry time.
- Do not use future data to qualify an entry.
"""

import json
import gzip
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

DATE = "2026-08-27"

# ------------------------------------------------------------------
# Locate data
# ------------------------------------------------------------------

ROOT = Path(".")
TICK_CANDIDATES = [
    Path(f"runtime/equity_socket_shadow/ticks_{DATE}_recovered.jsonl"),
    Path(f"runtime/equity_socket_shadow/ticks_{DATE}.jsonl"),
    Path(f"runtime/equity_socket_shadow/ticks_{DATE}.jsonl.gz"),
]

tick_file = next((p for p in TICK_CANDIDATES if p.exists()), None)

if tick_file is None:
    raise SystemExit("ABORT: no Aug-27 tick/depth file found")

print("TICK FILE =", tick_file)

# ------------------------------------------------------------------
# Load actual unique trade entries from trade history.
# Adaptively locate likely history file.
# ------------------------------------------------------------------

history_candidates = [
    Path("trade_history.jsonl"),
    Path("runtime/trade_history.jsonl"),
]

history_file = next((p for p in history_candidates if p.exists()), None)

if history_file is None:
    raise SystemExit("ABORT: trade history file not found")


def parse_dt(v):
    if not v:
        return None

    from datetime import timezone, timedelta

    try:
        dt = datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )

        # Raw socket timestamps are IST but have no
        # explicit timezone offset.
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone(timedelta(hours=5, minutes=30))
            )

        return dt

    except Exception:
        return None


# Group exit legs belonging to same signal/entry.
groups = {}

with history_file.open(errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        text = json.dumps(x, default=str)

        if DATE not in text:
            continue

        symbol = x.get("symbol")
        direction = x.get("direction") or x.get("side")
        entry = x.get("entry") or x.get("entry_price")
        signal_id = x.get("signal_id")

        if not symbol or not direction or entry is None:
            continue

        key = signal_id or (
            symbol,
            direction,
            str(x.get("entry_time")),
            float(entry),
        )

        g = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "direction": str(direction).upper(),
                "entry": float(entry),
                "entry_time": x.get("entry_time"),
                "order_submitted_at": x.get("order_submitted_at"),
                "signal_candle_close": x.get("signal_candle_close"),
                "pnl": 0.0,
                "legs": 0,
                "records": [],
            },
        )

        pnl = x.get("pnl")
        if pnl is not None:
            try:
                g["pnl"] += float(pnl)
            except Exception:
                pass

        g["legs"] += 1
        g["records"].append(x)

trades = list(groups.values())

# Exact decision timestamp preference:
# order submission -> signal close -> entry time
for t in trades:
    t["decision_dt"] = (
        parse_dt(t.get("order_submitted_at"))
        or parse_dt(t.get("signal_candle_close"))
        or parse_dt(t.get("entry_time"))
    )

trades = [t for t in trades if t["decision_dt"] is not None]
trades.sort(key=lambda x: x["decision_dt"])

print("UNIQUE ACTUAL ENTRIES =", len(trades))

# ------------------------------------------------------------------
# Get DI / EMA / ATR from existing recorded entry context where present.
# ------------------------------------------------------------------

for t in trades:
    r = t["records"][0]

    q = r.get("entry_quality_detail") or {}
    c = r.get("entry_context_detail") or {}

    t["atr"] = q.get("atr")
    t["ema_distance_atr"] = q.get("ema_distance_atr")

    # Some logs may already contain DI.
    t["adx"] = (
        c.get("adx_current")
        or r.get("adx")
        or r.get("adx_current")
    )

    t["plus_di"] = (
        c.get("plus_di")
        or c.get("+di")
        or r.get("plus_di")
        or r.get("+di")
    )

    t["minus_di"] = (
        c.get("minus_di")
        or c.get("-di")
        or r.get("minus_di")
        or r.get("-di")
    )


# ------------------------------------------------------------------
# If DI is not in trade records, reuse today's verified DI values
# from the historical ADX/DI replay output.
#
# These values came from:
# all_trades_adx_trend_2026-08-27.txt
# ------------------------------------------------------------------

KNOWN_DI = {
    ("VINCOFE", "BUY", 169.80): (39.92, 40.64, 4.20),
    ("TRITURBINE", "BUY", 588.00): (43.82, 37.38, 17.32),
    ("TATAPOWER", "SELL", 351.60): (53.06, 8.33, 55.10),
    ("MOSCHIP", "BUY", 219.11): (50.92, 44.94, 3.97),
    ("IFCI", "BUY", 87.02): (45.74, 34.12, 11.35),
    ("MOSCHIP", "BUY", 220.70): (40.07, 26.84, 18.03),
    ("LICHSGFIN", "BUY", 534.90): (35.65, 28.90, 14.28),
    ("OAL", "BUY", 424.15): (20.09, 21.95, 14.04),
    ("LICHSGFIN", "BUY", 537.75): (37.57, 32.60, 16.15),
    ("CGCL", "SELL", 248.08): (47.87, 11.93, 25.26),
    ("BHEL", "BUY", 433.15): (32.93, 26.62, 17.86),
    ("DCBBANK", "BUY", 216.52): (14.21, 16.07, 20.39),
    ("TDPOWERSYS", "SELL", 743.70): (24.60, 24.04, 23.19),
    ("HCC", "BUY", 25.54): (20.63, 22.72, 18.77),
    ("VISL", "SELL", 38.62): (34.97, 13.46, 28.51),
}

for t in trades:
    key = (
        t["symbol"],
        t["direction"],
        round(t["entry"], 2),
    )

    if key in KNOWN_DI:
        adx, pdi, mdi = KNOWN_DI[key]
        t["adx"] = adx
        t["plus_di"] = pdi
        t["minus_di"] = mdi


# ------------------------------------------------------------------
# Load depth ticks.
# ------------------------------------------------------------------

def open_ticks(path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return path.open("r", errors="replace")


depth_by_symbol = defaultdict(list)

with open_ticks(tick_file) as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        symbol = str(
            x.get("symbol") or ""
        ).strip().upper()

        depth = x.get("depth")

        if not symbol:
            continue

        if not isinstance(depth, dict):
            continue

        buys = depth.get("buy") or []
        sells = depth.get("sell") or []

        if not buys or not sells:
            continue

        # Use exchange timestamp first because this replay
        # needs the historical market state at decision time.
        ts = (
            x.get("exchange_timestamp")
            or x.get("received_at")
            or x.get("timestamp")
        )

        dt = parse_dt(ts)

        if dt is None:
            continue

        depth_by_symbol[symbol].append((dt, x))

for symbol in depth_by_symbol:
    depth_by_symbol[symbol].sort(key=lambda z: z[0])

print("DEPTH SYMBOLS =", len(depth_by_symbol))


# ------------------------------------------------------------------
# Five-level depth calculation.
#
# Calculate visible bid/ask quantity from the exact snapshots.
# Use a trailing 30-second window, matching today's depth design.
#
# Agreement:
# BUY:
#     imbalance >= +0.20
#     bullish persistence >= 70%
#
# SELL:
#     imbalance <= -0.20
#     bearish persistence >= 70%
#
# IMPORTANT:
# Only observations <= decision timestamp.
# ------------------------------------------------------------------

WINDOW_SECONDS = 30
IMBALANCE_THRESHOLD = 0.20
PERSISTENCE_THRESHOLD = 0.70


def side_rows(depth, names):
    for name in names:
        rows = depth.get(name)
        if isinstance(rows, list):
            return rows
    return []


def qty_sum(rows):
    total = 0.0
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue

        q = (
            row.get("quantity")
            or row.get("qty")
            or 0
        )

        try:
            total += float(q)
        except Exception:
            pass

    return total


def snapshot_imbalance(x):
    depth = x.get("depth") or {}

    bids = side_rows(
        depth,
        ["buy", "bids", "bid"],
    )
    asks = side_rows(
        depth,
        ["sell", "asks", "ask"],
    )

    b = qty_sum(bids)
    a = qty_sum(asks)

    total = b + a

    if total <= 0:
        return None

    return (b - a) / total


def depth_state(symbol, decision_dt, direction):
    rows = depth_by_symbol.get(symbol, [])

    if not rows:
        return {
            "available": False,
            "agree": False,
            "reason": "NO_DEPTH",
        }

    start = decision_dt - timedelta(seconds=WINDOW_SECONDS)

    vals = []

    for dt, x in rows:
        if dt > decision_dt:
            break

        if dt < start:
            continue

        imb = snapshot_imbalance(x)

        if imb is not None:
            vals.append(imb)

    if not vals:
        return {
            "available": False,
            "agree": False,
            "reason": "NO_WINDOW",
        }

    avg = sum(vals) / len(vals)

    if direction == "BUY":
        persistence = (
            sum(v >= IMBALANCE_THRESHOLD for v in vals)
            / len(vals)
        )

        agree = (
            avg >= IMBALANCE_THRESHOLD
            and persistence >= PERSISTENCE_THRESHOLD
        )

    else:
        persistence = (
            sum(v <= -IMBALANCE_THRESHOLD for v in vals)
            / len(vals)
        )

        agree = (
            avg <= -IMBALANCE_THRESHOLD
            and persistence >= PERSISTENCE_THRESHOLD
        )

    return {
        "available": True,
        "agree": agree,
        "avg_imbalance": avg,
        "persistence": persistence,
        "samples": len(vals),
    }


# ------------------------------------------------------------------
# Apply gates.
# ------------------------------------------------------------------

def di_agrees(t):
    p = t.get("plus_di")
    m = t.get("minus_di")

    if p is None or m is None:
        return False

    if t["direction"] == "BUY":
        return p > m

    return m > p


def ema_atr_pass(t):
    d = t.get("ema_distance_atr")

    if d is None:
        # Actual trades already passed the strategy active at that time.
        # Do NOT invent a rejection when historical value is unavailable.
        return True

    return float(d) <= 0.25


for t in trades:
    t["di_pass"] = di_agrees(t)
    t["ema_pass"] = ema_atr_pass(t)

    ds = depth_state(
        t["symbol"],
        t["decision_dt"],
        t["direction"],
    )

    t["depth"] = ds
    t["depth_pass"] = bool(ds.get("agree"))

    t["combined_pass"] = (
        t["ema_pass"]
        and t["di_pass"]
        and t["depth_pass"]
    )


# ------------------------------------------------------------------
# Report.
# ------------------------------------------------------------------

print()
print("=" * 150)
print("TRADE-BY-TRADE — EMA/ATR + DI + DEPTH AGREEMENT")
print("=" * 150)

accepted = []
rejected = []

for i, t in enumerate(trades, 1):
    ds = t["depth"]

    imb = ds.get("avg_imbalance")
    pers = ds.get("persistence")

    imb_s = "NA" if imb is None else f"{imb:+.3f}"
    pers_s = "NA" if pers is None else f"{pers*100:.1f}%"

    decision = "ACCEPT" if t["combined_pass"] else "REJECT"

    reasons = []

    if not t["ema_pass"]:
        reasons.append("EMA_ATR")

    if not t["di_pass"]:
        reasons.append("DI")

    if not t["depth_pass"]:
        reasons.append("DEPTH")

    if not reasons:
        reasons = ["ALL_PASS"]

    print(
        f"{i:02d}. "
        f"{t['symbol']:<12} "
        f"{t['direction']:<4} "
        f"PnL=₹{t['pnl']:+8.2f} "
        f"EMA={'PASS' if t['ema_pass'] else 'FAIL':<4} "
        f"+DI={str(t.get('plus_di')):<7} "
        f"-DI={str(t.get('minus_di')):<7} "
        f"DI={'PASS' if t['di_pass'] else 'FAIL':<4} "
        f"DEPTH_IMB={imb_s:<8} "
        f"PERSIST={pers_s:<7} "
        f"DEPTH={'PASS' if t['depth_pass'] else 'FAIL':<4} "
        f"=> {decision:<6} "
        f"{','.join(reasons)}"
    )

    if t["combined_pass"]:
        accepted.append(t)
    else:
        rejected.append(t)


def summary(name, rows):
    wins = [x for x in rows if x["pnl"] > 0]
    losses = [x for x in rows if x["pnl"] < 0]
    net = sum(x["pnl"] for x in rows)

    wr = (
        len(wins) / len(rows) * 100
        if rows else 0
    )

    print(
        f"{name:<35}"
        f"{len(rows):>8}"
        f"{len(wins):>8}"
        f"{len(losses):>8}"
        f"{wr:>10.2f}%"
        f"₹{net:>12.2f}"
    )


print()
print("=" * 100)
print("STRATEGY COMPARISON")
print("=" * 100)

print(
    f"{'RULE':<35}"
    f"{'TRADES':>8}"
    f"{'WINS':>8}"
    f"{'LOSSES':>8}"
    f"{'WIN%':>11}"
    f"{'NET PNL':>13}"
)

print("-" * 100)

summary("BASELINE ACTUAL", trades)

di_only = [
    t for t in trades
    if t["ema_pass"] and t["di_pass"]
]

summary("EMA<=0.25 + DI AGREES", di_only)

depth_only = [
    t for t in trades
    if t["ema_pass"] and t["depth_pass"]
]

summary("EMA<=0.25 + DEPTH AGREES", depth_only)

summary(
    "EMA<=0.25 + DI + DEPTH",
    accepted,
)

print()
print("=" * 100)
print("ACCEPTED TRADES")
print("=" * 100)

for t in accepted:
    print(
        f"{t['symbol']:<12} "
        f"{t['direction']:<4} "
        f"₹{t['pnl']:+.2f}"
    )

print()
print("=" * 100)
print("REJECTED TRADES — COUNTERFACTUAL PNL")
print("=" * 100)

avoided_losses = 0.0
lost_winners = 0.0

for t in rejected:
    if t["pnl"] < 0:
        avoided_losses += -t["pnl"]
    elif t["pnl"] > 0:
        lost_winners += t["pnl"]

    reasons = []

    if not t["ema_pass"]:
        reasons.append("EMA")
    if not t["di_pass"]:
        reasons.append("DI")
    if not t["depth_pass"]:
        reasons.append("DEPTH")

    print(
        f"{t['symbol']:<12} "
        f"{t['direction']:<4} "
        f"actual=₹{t['pnl']:+.2f} "
        f"rejected_by={'+'.join(reasons)}"
    )

print()
print("=" * 100)
print("FILTER IMPACT")
print("=" * 100)

print(f"Losses avoided       : ₹{avoided_losses:.2f}")
print(f"Profits sacrificed   : ₹{lost_winners:.2f}")
print(
    f"Net filtering benefit: "
    f"₹{avoided_losses - lost_winners:+.2f}"
)

print()
print(
    "ANALYSIS COMPLETE — "
    "NO BOT CONFIGURATION CHANGED"
)
