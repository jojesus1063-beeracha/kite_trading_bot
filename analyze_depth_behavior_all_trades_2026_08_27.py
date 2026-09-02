#!/usr/bin/env python3

"""
Aug 27 — Depth microstructure analysis for ALL actual trade entries.

PURPOSE
-------
Test whether dynamic depth behaviour separates profitable trades from losses.

NO BOT CHANGES.
NO ORDERS.
OFFLINE / HISTORICAL ANALYSIS ONLY.

For every unique actual entry:
    - use ONLY depth snapshots <= decision timestamp
    - trailing 30-second window
    - calculate:
        * average imbalance
        * first / last imbalance
        * imbalance change
        * imbalance slope
        * directional persistence
        * 5-level bid qty change
        * 5-level ask qty change
        * best bid movement
        * best ask movement
        * LTP movement
        * largest bid / ask wall
        * wall distance from LTP
        * raw depth agreement
        * strengthening depth
        * price confirmation
        * DI agreement
        * EMA/ATR state
    - compare winners vs losers
    - test alternative filters

IMPORTANT:
No future snapshots may qualify an entry.
"""

import json
import gzip
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from statistics import mean, median

DATE = "2026-08-27"

WINDOW_SECONDS = 30

RAW_IMBALANCE_THRESHOLD = 0.20
PERSISTENCE_THRESHOLD = 0.70

# Behaviour thresholds.
# Initially deliberately conservative.
MAX_OPPOSING_QTY_GROWTH = 0.50     # +50%
MIN_DIRECTIONAL_LTP_MOVE = 0.0     # must at least move in correct direction
MIN_IMBALANCE_CHANGE = -0.10       # allow small weakening, reject collapse
EMA_ATR_MAX = 0.25

TICK_CANDIDATES = [
    Path(
        "runtime/equity_socket_shadow/"
        "ticks_2026-08-27_recovered.jsonl"
    ),
    Path(
        "runtime/equity_socket_shadow/"
        "ticks_2026-08-27.jsonl.gz"
    ),
]

HISTORY_CANDIDATES = [
    Path("trade_history.jsonl"),
    Path("runtime/trade_history.jsonl"),
]

tick_file = next(
    (p for p in TICK_CANDIDATES if p.exists()),
    None
)

history_file = next(
    (p for p in HISTORY_CANDIDATES if p.exists()),
    None
)

if tick_file is None:
    raise SystemExit("ABORT: Aug-27 tick/depth file not found")

if history_file is None:
    raise SystemExit("ABORT: trade history file not found")


# ==================================================================
# TIME
# ==================================================================

IST = timezone(timedelta(hours=5, minutes=30))


def parse_dt(v):
    if not v:
        return None

    try:
        dt = datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )

        # Raw socket timestamps from today's capture were IST
        # despite not carrying an explicit offset.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)

        return dt.astimezone(IST)

    except Exception:
        return None


# ==================================================================
# HELPERS
# ==================================================================

def fnum(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def pct_change(first, last):
    if first is None or last is None:
        return None

    if first == 0:
        return None

    return (last - first) / abs(first)


def fmt(v, digits=3):
    if v is None:
        return "NA"

    return f"{v:+.{digits}f}"


def fmt_pct(v):
    if v is None:
        return "NA"

    return f"{v * 100:+.1f}%"


def open_ticks(path):
    if str(path).endswith(".gz"):
        return gzip.open(
            path,
            "rt",
            errors="replace"
        )

    return path.open(
        "r",
        errors="replace"
    )


# ==================================================================
# LOAD ACTUAL TRADES
# ==================================================================

groups = {}

with history_file.open("r", errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        # Keep Aug-27 records only.
        raw_date = (
            x.get("date")
            or str(x.get("entry_time") or "")[:10]
            or str(x.get("signal_candle_start") or "")[:10]
        )

        if DATE not in str(raw_date):
            continue

        symbol = x.get("symbol")
        direction = (
            x.get("direction")
            or x.get("side")
        )

        entry = fnum(
            x.get("entry")
            or x.get("entry_price")
        )

        if not symbol or not direction or entry is None:
            continue

        direction = str(direction).upper()

        signal_id = x.get("signal_id")

        # Prefer signal ID because scalp + runner are one entry.
        if signal_id:
            key = ("signal", signal_id)
        else:
            key = (
                symbol,
                direction,
                str(x.get("entry_time")),
                entry,
            )

        if key not in groups:
            groups[key] = {
                "records": [],
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
            }

        groups[key]["records"].append(x)


trades = []

for g in groups.values():
    records = g["records"]

    pnl = 0.0

    for r in records:
        v = fnum(
            r.get("pnl")
            if r.get("pnl") is not None
            else r.get("net_pnl")
        )

        if v is not None:
            pnl += v

    r = records[0]

    decision_dt = (
        parse_dt(r.get("order_submitted_at"))
        or parse_dt(r.get("signal_candle_close"))
        or parse_dt(r.get("entry_time"))
    )

    if decision_dt is None:
        continue

    q = r.get("entry_quality_detail") or {}
    c = r.get("entry_context_detail") or {}

    t = {
        **g,
        "decision_dt": decision_dt,
        "pnl": pnl,
        "winner": pnl > 0,

        "atr": fnum(q.get("atr")),
        "ema_distance_atr": fnum(
            q.get("ema_distance_atr")
        ),

        "adx": fnum(c.get("adx_current")),

        "plus_di": fnum(
            c.get("plus_di")
            or r.get("plus_di")
        ),

        "minus_di": fnum(
            c.get("minus_di")
            or r.get("minus_di")
        ),
    }

    trades.append(t)


trades.sort(
    key=lambda x: x["decision_dt"]
)

print("TICK FILE          =", tick_file)
print("HISTORY FILE       =", history_file)
print("UNIQUE ENTRIES     =", len(trades))


# ==================================================================
# DI FALLBACK
#
# Some history records did not store +DI/-DI directly.
#
# We already know today's prior ADX analysis produced these values.
# Include exact observed values only for matching actual entries.
# ==================================================================

KNOWN_DI = {
    ("VINCOFE", 169.80): (40.64, 4.20),
    ("TRITURBINE", 588.00): (37.38, 17.32),
    ("TATAPOWER", 351.60): (8.33, 55.10),
    ("MOSCHIP", 219.11): (44.94, 3.97),
    ("IFCI", 87.02): (34.12, 11.35),
    ("MOSCHIP", 220.70): (26.84, 18.03),
    ("LICHSGFIN", 534.90): (28.90, 14.28),
    ("OAL", 424.15): (21.95, 14.04),
    ("LICHSGFIN", 537.75): (32.60, 16.15),
    ("CGCL", 248.08): (11.93, 25.26),
    ("BHEL", 433.15): (26.62, 17.86),
    ("DCBBANK", 216.52): (16.07, 20.39),
    ("TDPOWERSYS", 743.70): (24.04, 23.19),
    ("HCC", 25.54): (22.72, 18.77),
    ("VISL", 38.62): (13.46, 28.51),
}

for t in trades:
    if (
        t["plus_di"] is None
        or t["minus_di"] is None
    ):
        key = (
            t["symbol"],
            round(t["entry"], 2)
        )

        vals = KNOWN_DI.get(key)

        if vals:
            t["plus_di"], t["minus_di"] = vals


# ==================================================================
# LOAD DEPTH
# ==================================================================

depth_by_symbol = defaultdict(list)

raw_records = 0
depth_records = 0

with open_ticks(tick_file) as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        raw_records += 1

        symbol = x.get("symbol")
        depth = x.get("depth")

        if not symbol:
            continue

        if not isinstance(depth, dict):
            continue

        if not depth.get("buy") or not depth.get("sell"):
            continue

        # IMPORTANT:
        # use exchange timestamp first.
        # received_at may have a different representation.
        ts = (
            x.get("exchange_timestamp")
            or x.get("timestamp")
            or x.get("received_at")
        )

        dt = parse_dt(ts)

        if dt is None:
            continue

        depth_records += 1

        depth_by_symbol[symbol].append(
            (dt, x)
        )


for symbol in depth_by_symbol:
    depth_by_symbol[symbol].sort(
        key=lambda z: z[0]
    )


print("RAW TICK RECORDS    =", raw_records)
print("VALID DEPTH RECORDS =", depth_records)
print("DEPTH SYMBOLS       =", len(depth_by_symbol))


# ==================================================================
# DEPTH CALCULATIONS
# ==================================================================

def qty_sum(rows):
    total = 0.0

    for row in rows[:5]:
        if not isinstance(row, dict):
            continue

        q = fnum(
            row.get("quantity")
            or row.get("qty")
        )

        if q is not None:
            total += q

    return total


def best_level(rows):
    if not rows:
        return None, None

    row = rows[0]

    return (
        fnum(row.get("price")),
        fnum(
            row.get("quantity")
            or row.get("qty")
        )
    )


def largest_wall(rows):
    best = None

    for row in rows[:5]:
        if not isinstance(row, dict):
            continue

        price = fnum(row.get("price"))
        qty = fnum(
            row.get("quantity")
            or row.get("qty")
        )

        if price is None or qty is None:
            continue

        if best is None or qty > best[1]:
            best = (price, qty)

    return best or (None, None)


def snapshot_values(x):
    depth = x.get("depth") or {}

    bids = depth.get("buy") or []
    asks = depth.get("sell") or []

    bid_qty = qty_sum(bids)
    ask_qty = qty_sum(asks)

    total = bid_qty + ask_qty

    imbalance = None

    if total > 0:
        imbalance = (
            bid_qty - ask_qty
        ) / total

    best_bid, best_bid_qty = best_level(bids)
    best_ask, best_ask_qty = best_level(asks)

    big_bid_price, big_bid_qty = largest_wall(bids)
    big_ask_price, big_ask_qty = largest_wall(asks)

    ltp = fnum(
        x.get("last_price")
        or x.get("ltp")
    )

    return {
        "ltp": ltp,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "imbalance": imbalance,

        "best_bid": best_bid,
        "best_bid_qty": best_bid_qty,

        "best_ask": best_ask,
        "best_ask_qty": best_ask_qty,

        "big_bid_price": big_bid_price,
        "big_bid_qty": big_bid_qty,

        "big_ask_price": big_ask_price,
        "big_ask_qty": big_ask_qty,
    }


def analyze_depth(t):
    symbol = t["symbol"]
    direction = t["direction"]
    decision_dt = t["decision_dt"]

    start = (
        decision_dt
        - timedelta(seconds=WINDOW_SECONDS)
    )

    rows = []

    for dt, x in depth_by_symbol.get(symbol, []):
        if dt > decision_dt:
            break

        if dt < start:
            continue

        v = snapshot_values(x)

        if v["imbalance"] is None:
            continue

        rows.append(
            (dt, v)
        )

    if not rows:
        return {
            "available": False,
            "snapshots": 0,
        }

    first_dt, first = rows[0]
    last_dt, last = rows[-1]

    imbalances = [
        v["imbalance"]
        for _, v in rows
        if v["imbalance"] is not None
    ]

    avg_imb = mean(imbalances)

    first_imb = first["imbalance"]
    last_imb = last["imbalance"]

    imb_change = (
        last_imb - first_imb
    )

    elapsed = (
        last_dt - first_dt
    ).total_seconds()

    imb_slope = None

    if elapsed > 0:
        imb_slope = imb_change / elapsed

    bid_change = pct_change(
        first["bid_qty"],
        last["bid_qty"]
    )

    ask_change = pct_change(
        first["ask_qty"],
        last["ask_qty"]
    )

    ltp_change = None
    ltp_change_pct = None

    if (
        first["ltp"] is not None
        and last["ltp"] is not None
    ):
        ltp_change = (
            last["ltp"] - first["ltp"]
        )

        if first["ltp"] != 0:
            ltp_change_pct = (
                ltp_change
                / first["ltp"]
            )

    best_bid_move = None

    if (
        first["best_bid"] is not None
        and last["best_bid"] is not None
    ):
        best_bid_move = (
            last["best_bid"]
            - first["best_bid"]
        )

    best_ask_move = None

    if (
        first["best_ask"] is not None
        and last["best_ask"] is not None
    ):
        best_ask_move = (
            last["best_ask"]
            - first["best_ask"]
        )

    if direction == "BUY":
        agreeing = [
            x >= RAW_IMBALANCE_THRESHOLD
            for x in imbalances
        ]

        raw_agree = (
            avg_imb >= RAW_IMBALANCE_THRESHOLD
        )

        directional_imb_change = imb_change

        opposing_qty_growth = ask_change

        price_confirm = (
            ltp_change is not None
            and ltp_change > MIN_DIRECTIONAL_LTP_MOVE
        )

        quote_confirm = (
            best_bid_move is not None
            and best_bid_move >= 0
        )

    else:
        agreeing = [
            x <= -RAW_IMBALANCE_THRESHOLD
            for x in imbalances
        ]

        raw_agree = (
            avg_imb <= -RAW_IMBALANCE_THRESHOLD
        )

        # For SELL, increasingly negative imbalance
        # is strengthening.
        directional_imb_change = -imb_change

        # BUY liquidity is opposition to SELL.
        opposing_qty_growth = bid_change

        price_confirm = (
            ltp_change is not None
            and ltp_change < -MIN_DIRECTIONAL_LTP_MOVE
        )

        quote_confirm = (
            best_ask_move is not None
            and best_ask_move <= 0
        )

    persistence = (
        sum(agreeing) / len(agreeing)
        if agreeing
        else 0.0
    )

    persistence_pass = (
        persistence >= PERSISTENCE_THRESHOLD
    )

    imbalance_stable = (
        directional_imb_change
        >= MIN_IMBALANCE_CHANGE
    )

    opposition_ok = (
        opposing_qty_growth is None
        or opposing_qty_growth
        <= MAX_OPPOSING_QTY_GROWTH
    )

    strengthening = (
        raw_agree
        and persistence_pass
        and imbalance_stable
        and opposition_ok
    )

    strong_with_price = (
        strengthening
        and price_confirm
    )

    strong_with_quote = (
        strengthening
        and quote_confirm
    )

    full_behavior = (
        strengthening
        and price_confirm
        and quote_confirm
    )

    final_ltp = last["ltp"]

    big_bid_distance_pct = None
    big_ask_distance_pct = None

    if final_ltp:
        if last["big_bid_price"] is not None:
            big_bid_distance_pct = (
                final_ltp
                - last["big_bid_price"]
            ) / final_ltp

        if last["big_ask_price"] is not None:
            big_ask_distance_pct = (
                last["big_ask_price"]
                - final_ltp
            ) / final_ltp

    return {
        "available": True,
        "snapshots": len(rows),

        "first_time": first_dt,
        "last_time": last_dt,

        "first_ltp": first["ltp"],
        "last_ltp": last["ltp"],
        "ltp_change": ltp_change,
        "ltp_change_pct": ltp_change_pct,

        "avg_imbalance": avg_imb,
        "first_imbalance": first_imb,
        "last_imbalance": last_imb,
        "imbalance_change": imb_change,
        "directional_imb_change":
            directional_imb_change,
        "imbalance_slope": imb_slope,

        "persistence": persistence,

        "first_bid_qty": first["bid_qty"],
        "last_bid_qty": last["bid_qty"],
        "bid_qty_change_pct": bid_change,

        "first_ask_qty": first["ask_qty"],
        "last_ask_qty": last["ask_qty"],
        "ask_qty_change_pct": ask_change,

        "first_best_bid": first["best_bid"],
        "last_best_bid": last["best_bid"],
        "best_bid_move": best_bid_move,

        "first_best_ask": first["best_ask"],
        "last_best_ask": last["best_ask"],
        "best_ask_move": best_ask_move,

        "big_bid_price": last["big_bid_price"],
        "big_bid_qty": last["big_bid_qty"],

        "big_ask_price": last["big_ask_price"],
        "big_ask_qty": last["big_ask_qty"],

        "big_bid_distance_pct":
            big_bid_distance_pct,

        "big_ask_distance_pct":
            big_ask_distance_pct,

        "raw_agree": raw_agree,
        "persistence_pass": persistence_pass,
        "imbalance_stable": imbalance_stable,
        "opposition_ok": opposition_ok,
        "price_confirm": price_confirm,
        "quote_confirm": quote_confirm,

        "strengthening": strengthening,
        "strong_with_price": strong_with_price,
        "strong_with_quote": strong_with_quote,
        "full_behavior": full_behavior,
    }


# ==================================================================
# ANALYZE EVERY TRADE
# ==================================================================

for t in trades:
    t["depth"] = analyze_depth(t)

    pdi = t["plus_di"]
    mdi = t["minus_di"]

    if pdi is None or mdi is None:
        t["di_agree"] = None

    elif t["direction"] == "BUY":
        t["di_agree"] = pdi > mdi

    else:
        t["di_agree"] = mdi > pdi

    ed = t["ema_distance_atr"]

    t["ema_pass"] = (
        ed is not None
        and ed <= EMA_ATR_MAX
    )


# ==================================================================
# TRADE-BY-TRADE OUTPUT
# ==================================================================

print()
print("=" * 170)
print(
    "TRADE-BY-TRADE DEPTH MICROSTRUCTURE"
)
print("=" * 170)

for i, t in enumerate(trades, 1):
    d = t["depth"]

    print()
    print(
        f"{i:02d}. "
        f"{t['decision_dt'].strftime('%H:%M:%S')} "
        f"{t['symbol']:<12} "
        f"{t['direction']:<4} "
        f"PnL=₹{t['pnl']:+8.2f}"
    )

    print(
        f"    EMA_ATR="
        f"{t['ema_distance_atr'] if t['ema_distance_atr'] is not None else 'NA'} "
        f"EMA_PASS={t['ema_pass']} "
        f"+DI={t['plus_di']} "
        f"-DI={t['minus_di']} "
        f"DI_PASS={t['di_agree']}"
    )

    if not d["available"]:
        print(
            "    DEPTH=UNAVAILABLE"
        )
        continue

    print(
        f"    snapshots={d['snapshots']} "
        f"IMB avg={fmt(d['avg_imbalance'])} "
        f"first={fmt(d['first_imbalance'])} "
        f"last={fmt(d['last_imbalance'])} "
        f"change={fmt(d['imbalance_change'])} "
        f"directional_change="
        f"{fmt(d['directional_imb_change'])}"
    )

    print(
        f"    persistence="
        f"{d['persistence'] * 100:.1f}% "
        f"RAW_AGREE={d['raw_agree']} "
        f"STABLE={d['imbalance_stable']}"
    )

    print(
        f"    BID_QTY "
        f"{d['first_bid_qty']:.0f}"
        f"->{d['last_bid_qty']:.0f} "
        f"({fmt_pct(d['bid_qty_change_pct'])}) "
        f"ASK_QTY "
        f"{d['first_ask_qty']:.0f}"
        f"->{d['last_ask_qty']:.0f} "
        f"({fmt_pct(d['ask_qty_change_pct'])})"
    )

    print(
        f"    LTP "
        f"{d['first_ltp']}"
        f"->{d['last_ltp']} "
        f"move={fmt(d['ltp_change'], 2)} "
        f"PRICE_CONFIRM={d['price_confirm']}"
    )

    print(
        f"    BEST_BID "
        f"{d['first_best_bid']}"
        f"->{d['last_best_bid']} "
        f"move={fmt(d['best_bid_move'], 2)} | "
        f"BEST_ASK "
        f"{d['first_best_ask']}"
        f"->{d['last_best_ask']} "
        f"move={fmt(d['best_ask_move'], 2)} "
        f"QUOTE_CONFIRM={d['quote_confirm']}"
    )

    print(
        f"    FINAL WALLS: "
        f"BID ₹{d['big_bid_price']} "
        f"x{d['big_bid_qty']} "
        f"ASK ₹{d['big_ask_price']} "
        f"x{d['big_ask_qty']}"
    )

    print(
        f"    DEPTH_RAW={d['raw_agree']} "
        f"STRENGTHENING={d['strengthening']} "
        f"+PRICE={d['strong_with_price']} "
        f"FULL_BEHAVIOR={d['full_behavior']}"
    )


# ==================================================================
# STRATEGY COMPARISON
# ==================================================================

def test_rule(name, fn):
    accepted = [
        t for t in trades
        if fn(t)
    ]

    rejected = [
        t for t in trades
        if not fn(t)
    ]

    wins = sum(
        1 for t in accepted
        if t["pnl"] > 0
    )

    losses = sum(
        1 for t in accepted
        if t["pnl"] < 0
    )

    pnl = sum(
        t["pnl"]
        for t in accepted
    )

    avoided_losses = -sum(
        t["pnl"]
        for t in rejected
        if t["pnl"] < 0
    )

    sacrificed_profit = sum(
        t["pnl"]
        for t in rejected
        if t["pnl"] > 0
    )

    winrate = (
        wins / len(accepted) * 100
        if accepted
        else 0
    )

    return {
        "name": name,
        "accepted": accepted,
        "rejected": rejected,
        "trades": len(accepted),
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "pnl": pnl,
        "avoided_losses": avoided_losses,
        "sacrificed_profit": sacrificed_profit,
    }


rules = [

    test_rule(
        "BASELINE",
        lambda t: True
    ),

    test_rule(
        "DI AGREES",
        lambda t:
            t["di_agree"] is True
    ),

    test_rule(
        "RAW DEPTH",
        lambda t:
            t["depth"].get("raw_agree") is True
            and
            t["depth"].get("persistence_pass") is True
    ),

    test_rule(
        "DEPTH STRENGTHENING",
        lambda t:
            t["depth"].get("strengthening") is True
    ),

    test_rule(
        "DEPTH + PRICE",
        lambda t:
            t["depth"].get("strong_with_price") is True
    ),

    test_rule(
        "DEPTH FULL BEHAVIOR",
        lambda t:
            t["depth"].get("full_behavior") is True
    ),

    test_rule(
        "DI + DEPTH",
        lambda t:
            t["di_agree"] is True
            and
            t["depth"].get("strengthening") is True
    ),

    test_rule(
        "DI + DEPTH + PRICE",
        lambda t:
            t["di_agree"] is True
            and
            t["depth"].get("strong_with_price") is True
    ),

    test_rule(
        "EMA + DI + DEPTH",
        lambda t:
            t["ema_pass"] is True
            and
            t["di_agree"] is True
            and
            t["depth"].get("strengthening") is True
    ),

    test_rule(
        "EMA + DI + DEPTH + PRICE",
        lambda t:
            t["ema_pass"] is True
            and
            t["di_agree"] is True
            and
            t["depth"].get("strong_with_price") is True
    ),
]


print()
print("=" * 145)
print("STRATEGY COMPARISON")
print("=" * 145)

print(
    f"{'RULE':<34}"
    f"{'TRADES':>8}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>14}"
    f"{'LOSS AVOID':>14}"
    f"{'PROFIT LOST':>14}"
)

print("-" * 145)

for r in rules:
    print(
        f"{r['name']:<34}"
        f"{r['trades']:>8}"
        f"{r['wins']:>6}"
        f"{r['losses']:>6}"
        f"{r['winrate']:>9.2f}%"
        f"₹{r['pnl']:>+12.2f}"
        f"₹{r['avoided_losses']:>+12.2f}"
        f"₹{r['sacrificed_profit']:>+12.2f}"
    )


# ==================================================================
# WINNERS VS LOSERS
# ==================================================================

print()
print("=" * 145)
print("WINNERS VS LOSERS — DEPTH CHARACTERISTICS")
print("=" * 145)


def avg_metric(items, path):
    vals = []

    for t in items:
        d = t["depth"]

        v = d.get(path)

        if v is not None:
            vals.append(v)

    return mean(vals) if vals else None


winners = [
    t for t in trades
    if t["pnl"] > 0
    and t["depth"].get("available")
]

losers = [
    t for t in trades
    if t["pnl"] < 0
    and t["depth"].get("available")
]


metrics = [
    ("AVG IMBALANCE", "avg_imbalance"),
    ("DIRECTIONAL IMB CHANGE", "directional_imb_change"),
    ("BID QTY CHANGE", "bid_qty_change_pct"),
    ("ASK QTY CHANGE", "ask_qty_change_pct"),
    ("LTP CHANGE %", "ltp_change_pct"),
    ("PERSISTENCE", "persistence"),
]


print(
    f"{'METRIC':<32}"
    f"{'WINNERS':>18}"
    f"{'LOSERS':>18}"
)

print("-" * 70)

for label, key in metrics:
    w = avg_metric(winners, key)
    l = avg_metric(losers, key)

    print(
        f"{label:<32}"
        f"{fmt(w, 4):>18}"
        f"{fmt(l, 4):>18}"
    )


# ==================================================================
# ACCEPTED / REJECTED DETAILS FOR BEST RULES
# ==================================================================

for rule_name in [
    "DI + DEPTH",
    "DI + DEPTH + PRICE",
    "EMA + DI + DEPTH",
    "EMA + DI + DEPTH + PRICE",
]:

    r = next(
        x for x in rules
        if x["name"] == rule_name
    )

    print()
    print("=" * 145)
    print(rule_name)
    print("=" * 145)

    print("ACCEPTED")
    print("-" * 80)

    for t in r["accepted"]:
        d = t["depth"]

        print(
            f"{t['symbol']:<12} "
            f"{t['direction']:<4} "
            f"PnL=₹{t['pnl']:+8.2f} "
            f"IMB={fmt(d.get('avg_imbalance'))} "
            f"dIMB={fmt(d.get('directional_imb_change'))} "
            f"PERSIST="
            f"{(d.get('persistence') or 0)*100:.1f}% "
            f"LTP_MOVE={fmt(d.get('ltp_change'),2)}"
        )

    print()
    print("REJECTED")
    print("-" * 80)

    for t in r["rejected"]:
        d = t["depth"]

        print(
            f"{t['symbol']:<12} "
            f"{t['direction']:<4} "
            f"PnL=₹{t['pnl']:+8.2f} "
            f"EMA={t['ema_pass']} "
            f"DI={t['di_agree']} "
            f"DEPTH="
            f"{d.get('strengthening')} "
            f"PRICE="
            f"{d.get('price_confirm')}"
        )


# ==================================================================
# OAL SPECIAL CHECK
# ==================================================================

print()
print("=" * 145)
print("OAL VALIDATION")
print("=" * 145)

for t in trades:
    if (
        t["symbol"] == "OAL"
        and abs(t["entry"] - 424.15) < 0.05
    ):
        d = t["depth"]

        print(
            "PnL                    =",
            f"₹{t['pnl']:+.2f}"
        )

        print(
            "Average imbalance      =",
            fmt(d.get("avg_imbalance"))
        )

        print(
            "Imbalance change       =",
            fmt(d.get("imbalance_change"))
        )

        print(
            "Directional change     =",
            fmt(d.get("directional_imb_change"))
        )

        print(
            "Bid qty change         =",
            fmt_pct(d.get("bid_qty_change_pct"))
        )

        print(
            "Ask qty change         =",
            fmt_pct(d.get("ask_qty_change_pct"))
        )

        print(
            "LTP move               =",
            fmt(d.get("ltp_change"), 2)
        )

        print(
            "Raw depth pass         =",
            d.get("raw_agree")
        )

        print(
            "Persistence pass       =",
            d.get("persistence_pass")
        )

        print(
            "Imbalance stable       =",
            d.get("imbalance_stable")
        )

        print(
            "Opposing liquidity OK  =",
            d.get("opposition_ok")
        )

        print(
            "Price confirms         =",
            d.get("price_confirm")
        )

        print(
            "Quote confirms         =",
            d.get("quote_confirm")
        )

        print(
            "Strengthening depth    =",
            d.get("strengthening")
        )

        print(
            "Full behavior pass     =",
            d.get("full_behavior")
        )


print()
print("=" * 145)
print(
    "ANALYSIS COMPLETE — NO BOT CONFIGURATION CHANGED"
)
print("=" * 145)



# ==================================================================
# CORRECTED 8-POINT MICROSTRUCTURE SCORE
# ==================================================================

print()
print("=" * 150)
print("CORRECTED 8-POINT MICROSTRUCTURE SCORE")
print("=" * 150)

def safe_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_trade(t):
    d = t.get("depth") or {}
    direction = str(t.get("direction", "")).upper()

    avg_imb = safe_num(d.get("avg_imbalance"))
    dimb = safe_num(d.get("directional_imb_change"))
    persistence = safe_num(d.get("persistence"))
    bid_change = safe_num(d.get("bid_qty_change_pct"))
    ask_change = safe_num(d.get("ask_qty_change_pct"))

    # --------------------------------------------------------------
    # 1. EMA / ATR accepted
    # --------------------------------------------------------------
    ema_ok = t.get("ema_pass") is True

    # --------------------------------------------------------------
    # 2. DI agrees with direction
    # --------------------------------------------------------------
    di_ok = t.get("di_agree") is True

    # --------------------------------------------------------------
    # 3. Raw depth direction agrees
    # Existing script already calculates this correctly.
    # --------------------------------------------------------------
    depth_ok = d.get("raw_agree") is True

    # --------------------------------------------------------------
    # 4. Persistence >= configured threshold
    # --------------------------------------------------------------
    persistence_ok = d.get("persistence_pass") is True

    # --------------------------------------------------------------
    # 5. Imbalance is strengthening in TRADE direction
    #
    # analyze_depth() already converts this into directional form:
    # positive = improving for intended direction
    # negative = deteriorating
    # --------------------------------------------------------------
    strengthening_ok = (
        dimb is not None and dimb > 0
    )

    # --------------------------------------------------------------
    # 6. Bid/ask quantity evolution supports direction
    #
    # BUY:
    #   bid should strengthen relative to ask
    #
    # SELL:
    #   ask should strengthen relative to bid
    # --------------------------------------------------------------
    book_evolution_ok = False

    if bid_change is not None and ask_change is not None:
        if direction == "BUY":
            book_evolution_ok = bid_change > ask_change
        elif direction == "SELL":
            book_evolution_ok = ask_change > bid_change

    # --------------------------------------------------------------
    # 7. Actual traded price confirms direction
    # --------------------------------------------------------------
    price_ok = d.get("price_confirm") is True

    # --------------------------------------------------------------
    # 8. Best bid/ask quote movement confirms direction
    # --------------------------------------------------------------
    quote_ok = d.get("quote_confirm") is True

    components = {
        "EMA": ema_ok,
        "DI": di_ok,
        "DEPTH": depth_ok,
        "PERSIST": persistence_ok,
        "STRENGTHEN": strengthening_ok,
        "BOOK": book_evolution_ok,
        "PRICE": price_ok,
        "QUOTE": quote_ok,
    }

    score = sum(bool(v) for v in components.values())

    return score, components


scored = []

for t in trades:
    score, components = score_trade(t)

    t["_micro_score"] = score
    t["_micro_components"] = components

    scored.append(t)


# ==================================================================
# TRADE BY TRADE
# ==================================================================

print()
print("TRADE-BY-TRADE")
print("-" * 150)

for i, t in enumerate(scored, 1):

    d = t.get("depth") or {}
    c = t["_micro_components"]

    pnl = float(t.get("pnl") or 0.0)

    flags = " ".join(
        f"{k}={'Y' if v else 'N'}"
        for k, v in c.items()
    )

    print(
        f"{i:02d}. "
        f"{t.get('symbol',''):<12} "
        f"{t.get('direction',''):<4} "
        f"PnL=₹{pnl:+8.2f} "
        f"SCORE={t['_micro_score']}/8 | "
        f"{flags}"
    )

    print(
        "    "
        f"IMB={d.get('avg_imbalance')} "
        f"dIMB={d.get('directional_imb_change')} "
        f"PERSIST={d.get('persistence')} "
        f"BIDchg={d.get('bid_qty_change_pct')} "
        f"ASKchg={d.get('ask_qty_change_pct')} "
        f"LTPchg={d.get('ltp_change')}"
    )


# ==================================================================
# SCORE DISTRIBUTION
# ==================================================================

print()
print("=" * 150)
print("SCORE DISTRIBUTION — EXACT SCORE")
print("=" * 150)

print(
    f"{'SCORE':>7} "
    f"{'TRADES':>8} "
    f"{'W':>5} "
    f"{'L':>5} "
    f"{'WIN%':>9} "
    f"{'NET PNL':>14}"
)

print("-" * 60)

for exact_score in range(9):

    selected = [
        t for t in scored
        if t["_micro_score"] == exact_score
    ]

    if not selected:
        continue

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    win_rate = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"{exact_score:>7}/8 "
        f"{len(selected):>8} "
        f"{wins:>5} "
        f"{losses:>5} "
        f"{win_rate:>8.2f}% "
        f"₹{pnl:>+12.2f}"
    )


# ==================================================================
# THRESHOLD SWEEP
# ==================================================================

print()
print("=" * 150)
print("MINIMUM SCORE THRESHOLD SWEEP")
print("=" * 150)

print(
    f"{'RULE':<15}"
    f"{'TRADES':>8}"
    f"{'REJECT':>9}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>15}"
)

print("-" * 75)

baseline_pnl = sum(
    float(t.get("pnl") or 0)
    for t in scored
)

for threshold in range(0, 9):

    selected = [
        t for t in scored
        if t["_micro_score"] >= threshold
    ]

    rejected = len(scored) - len(selected)

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    win_rate = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"SCORE >= {threshold:<5}"
        f"{len(selected):>8}"
        f"{rejected:>9}"
        f"{wins:>6}"
        f"{losses:>6}"
        f"{win_rate:>9.2f}%"
        f"₹{pnl:>+13.2f}"
    )


# ==================================================================
# WINNER / LOSER SCORE COMPARISON
# ==================================================================

winners = [
    t for t in scored
    if float(t.get("pnl") or 0) > 0
]

losers = [
    t for t in scored
    if float(t.get("pnl") or 0) < 0
]

print()
print("=" * 150)
print("WINNER VS LOSER SCORE")
print("=" * 150)

if winners:
    print(
        "WINNERS:",
        len(winners),
        "| average score =",
        round(
            sum(t["_micro_score"] for t in winners)
            / len(winners),
            3
        )
    )

if losers:
    print(
        "LOSERS :",
        len(losers),
        "| average score =",
        round(
            sum(t["_micro_score"] for t in losers)
            / len(losers),
            3
        )
    )


# ==================================================================
# COMPONENT EFFECTIVENESS
# ==================================================================

print()
print("=" * 150)
print("INDIVIDUAL COMPONENT EFFECTIVENESS")
print("=" * 150)

component_names = [
    "EMA",
    "DI",
    "DEPTH",
    "PERSIST",
    "STRENGTHEN",
    "BOOK",
    "PRICE",
    "QUOTE",
]

for name in component_names:

    selected = [
        t for t in scored
        if t["_micro_components"][name]
    ]

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    wr = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"{name:<12} "
        f"trades={len(selected):>2} "
        f"W={wins:>2} "
        f"L={losses:>2} "
        f"WR={wr:>6.2f}% "
        f"PnL=₹{pnl:+.2f}"
    )


print()
print("=" * 150)
print(
    "CORRECTED SCORE ANALYSIS COMPLETE — "
    "NO LIVE CONFIGURATION CHANGED"
)
print("=" * 150)


# ==================================================================
# CORRECTED 8-POINT MICROSTRUCTURE SCORE
# ==================================================================

print()
print("=" * 150)
print("CORRECTED 8-POINT MICROSTRUCTURE SCORE")
print("=" * 150)

def safe_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_trade(t):
    d = t.get("depth") or {}
    direction = str(t.get("direction", "")).upper()

    avg_imb = safe_num(d.get("avg_imbalance"))
    dimb = safe_num(d.get("directional_imb_change"))
    persistence = safe_num(d.get("persistence"))
    bid_change = safe_num(d.get("bid_qty_change_pct"))
    ask_change = safe_num(d.get("ask_qty_change_pct"))

    # --------------------------------------------------------------
    # 1. EMA / ATR accepted
    # --------------------------------------------------------------
    ema_ok = t.get("ema_pass") is True

    # --------------------------------------------------------------
    # 2. DI agrees with direction
    # --------------------------------------------------------------
    di_ok = t.get("di_agree") is True

    # --------------------------------------------------------------
    # 3. Raw depth direction agrees
    # Existing script already calculates this correctly.
    # --------------------------------------------------------------
    depth_ok = d.get("raw_agree") is True

    # --------------------------------------------------------------
    # 4. Persistence >= configured threshold
    # --------------------------------------------------------------
    persistence_ok = d.get("persistence_pass") is True

    # --------------------------------------------------------------
    # 5. Imbalance is strengthening in TRADE direction
    #
    # analyze_depth() already converts this into directional form:
    # positive = improving for intended direction
    # negative = deteriorating
    # --------------------------------------------------------------
    strengthening_ok = (
        dimb is not None and dimb > 0
    )

    # --------------------------------------------------------------
    # 6. Bid/ask quantity evolution supports direction
    #
    # BUY:
    #   bid should strengthen relative to ask
    #
    # SELL:
    #   ask should strengthen relative to bid
    # --------------------------------------------------------------
    book_evolution_ok = False

    if bid_change is not None and ask_change is not None:
        if direction == "BUY":
            book_evolution_ok = bid_change > ask_change
        elif direction == "SELL":
            book_evolution_ok = ask_change > bid_change

    # --------------------------------------------------------------
    # 7. Actual traded price confirms direction
    # --------------------------------------------------------------
    price_ok = d.get("price_confirm") is True

    # --------------------------------------------------------------
    # 8. Best bid/ask quote movement confirms direction
    # --------------------------------------------------------------
    quote_ok = d.get("quote_confirm") is True

    components = {
        "EMA": ema_ok,
        "DI": di_ok,
        "DEPTH": depth_ok,
        "PERSIST": persistence_ok,
        "STRENGTHEN": strengthening_ok,
        "BOOK": book_evolution_ok,
        "PRICE": price_ok,
        "QUOTE": quote_ok,
    }

    score = sum(bool(v) for v in components.values())

    return score, components


scored = []

for t in trades:
    score, components = score_trade(t)

    t["_micro_score"] = score
    t["_micro_components"] = components

    scored.append(t)


# ==================================================================
# TRADE BY TRADE
# ==================================================================

print()
print("TRADE-BY-TRADE")
print("-" * 150)

for i, t in enumerate(scored, 1):

    d = t.get("depth") or {}
    c = t["_micro_components"]

    pnl = float(t.get("pnl") or 0.0)

    flags = " ".join(
        f"{k}={'Y' if v else 'N'}"
        for k, v in c.items()
    )

    print(
        f"{i:02d}. "
        f"{t.get('symbol',''):<12} "
        f"{t.get('direction',''):<4} "
        f"PnL=₹{pnl:+8.2f} "
        f"SCORE={t['_micro_score']}/8 | "
        f"{flags}"
    )

    print(
        "    "
        f"IMB={d.get('avg_imbalance')} "
        f"dIMB={d.get('directional_imb_change')} "
        f"PERSIST={d.get('persistence')} "
        f"BIDchg={d.get('bid_qty_change_pct')} "
        f"ASKchg={d.get('ask_qty_change_pct')} "
        f"LTPchg={d.get('ltp_change')}"
    )


# ==================================================================
# SCORE DISTRIBUTION
# ==================================================================

print()
print("=" * 150)
print("SCORE DISTRIBUTION — EXACT SCORE")
print("=" * 150)

print(
    f"{'SCORE':>7} "
    f"{'TRADES':>8} "
    f"{'W':>5} "
    f"{'L':>5} "
    f"{'WIN%':>9} "
    f"{'NET PNL':>14}"
)

print("-" * 60)

for exact_score in range(9):

    selected = [
        t for t in scored
        if t["_micro_score"] == exact_score
    ]

    if not selected:
        continue

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    win_rate = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"{exact_score:>7}/8 "
        f"{len(selected):>8} "
        f"{wins:>5} "
        f"{losses:>5} "
        f"{win_rate:>8.2f}% "
        f"₹{pnl:>+12.2f}"
    )


# ==================================================================
# THRESHOLD SWEEP
# ==================================================================

print()
print("=" * 150)
print("MINIMUM SCORE THRESHOLD SWEEP")
print("=" * 150)

print(
    f"{'RULE':<15}"
    f"{'TRADES':>8}"
    f"{'REJECT':>9}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>10}"
    f"{'NET PNL':>15}"
)

print("-" * 75)

baseline_pnl = sum(
    float(t.get("pnl") or 0)
    for t in scored
)

for threshold in range(0, 9):

    selected = [
        t for t in scored
        if t["_micro_score"] >= threshold
    ]

    rejected = len(scored) - len(selected)

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    win_rate = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"SCORE >= {threshold:<5}"
        f"{len(selected):>8}"
        f"{rejected:>9}"
        f"{wins:>6}"
        f"{losses:>6}"
        f"{win_rate:>9.2f}%"
        f"₹{pnl:>+13.2f}"
    )


# ==================================================================
# WINNER / LOSER SCORE COMPARISON
# ==================================================================

winners = [
    t for t in scored
    if float(t.get("pnl") or 0) > 0
]

losers = [
    t for t in scored
    if float(t.get("pnl") or 0) < 0
]

print()
print("=" * 150)
print("WINNER VS LOSER SCORE")
print("=" * 150)

if winners:
    print(
        "WINNERS:",
        len(winners),
        "| average score =",
        round(
            sum(t["_micro_score"] for t in winners)
            / len(winners),
            3
        )
    )

if losers:
    print(
        "LOSERS :",
        len(losers),
        "| average score =",
        round(
            sum(t["_micro_score"] for t in losers)
            / len(losers),
            3
        )
    )


# ==================================================================
# COMPONENT EFFECTIVENESS
# ==================================================================

print()
print("=" * 150)
print("INDIVIDUAL COMPONENT EFFECTIVENESS")
print("=" * 150)

component_names = [
    "EMA",
    "DI",
    "DEPTH",
    "PERSIST",
    "STRENGTHEN",
    "BOOK",
    "PRICE",
    "QUOTE",
]

for name in component_names:

    selected = [
        t for t in scored
        if t["_micro_components"][name]
    ]

    wins = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) > 0
    )

    losses = sum(
        1 for t in selected
        if float(t.get("pnl") or 0) < 0
    )

    pnl = sum(
        float(t.get("pnl") or 0)
        for t in selected
    )

    wr = (
        wins / len(selected) * 100
        if selected else 0
    )

    print(
        f"{name:<12} "
        f"trades={len(selected):>2} "
        f"W={wins:>2} "
        f"L={losses:>2} "
        f"WR={wr:>6.2f}% "
        f"PnL=₹{pnl:+.2f}"
    )


print()
print("=" * 150)
print(
    "CORRECTED SCORE ANALYSIS COMPLETE — "
    "NO LIVE CONFIGURATION CHANGED"
)
print("=" * 150)
