#!/usr/bin/env python3

import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

DATE = "2026-08-27"

SIGNALS_FILE = Path(
    "runtime/opening_depth_backtest_2026-08-27.txt"
)

TICKS_FILE = Path(
    "runtime/equity_socket_shadow/"
    "ticks_2026-08-27_recovered.jsonl"
)

CAPITAL = 5000.0
RISK_PCT = 2.0
MAX_OPEN = 3
MAX_TRADES = 7

STOP_PCT = 0.45 / 100.0

# Exact paper entry-quality requirement.
MAX_EMA_DISTANCE_ATR = 0.25

# Current entry timeframe.
CANDLE_MINUTES = 3

# ATR period used by the strategy.
ATR_PERIOD = 14
EMA_PERIOD = 9

# Hybrid:
# scalp exits at +1R
# runner exits at +2R
SCALP_R = 1.0
RUNNER_R = 2.0


def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        return None


# -------------------------------------------------------
# Parse chronological depth signals
# -------------------------------------------------------

pat = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+"
    r"(\S+)\s+"
    r"(BUY|SELL)\s+"
    r"(STRONG|EXTREME)\s+"
    r"([+-]?\d+\.\d+)\s+"
    r"(\d+\.\d+)%\s+"
    r"(\d+\.\d+)"
)

signals = []

for line in SIGNALS_FILE.read_text(
    errors="replace"
).splitlines():

    m = pat.match(line.strip())

    if not m:
        continue

    dt = datetime.fromisoformat(
        f"{DATE}T{m.group(1)}"
    )

    signals.append(
        {
            "dt": dt,
            "symbol": m.group(2),
            "side": m.group(3),
            "class": m.group(4),
            "imb": float(m.group(5)),
            "pers": float(m.group(6)),
            "signal_entry": float(m.group(7)),
        }
    )

signals.sort(
    key=lambda x: (
        x["dt"],
        # deterministic tie-break:
        # strongest absolute imbalance first
        -abs(x["imb"]),
        -x["pers"],
        x["symbol"],
    )
)

print("=" * 120)
print("FIRST-7 DEPTH STRATEGY EXACT TICK REPLAY")
print("=" * 120)
print()
print("Signal tie-break = strongest |imbalance|, then persistence")
print()


# -------------------------------------------------------
# Load prices for signal symbols
# -------------------------------------------------------

needed = {s["symbol"] for s in signals}

ticks = defaultdict(list)

with TICKS_FILE.open(
    "r",
    errors="replace",
) as f:

    for line in f:

        try:
            x = json.loads(line)
        except Exception:
            continue

        symbol = str(
            x.get("symbol")
            or x.get("tradingsymbol")
            or ""
        ).upper()

        if symbol not in needed:
            continue

        dt = None

        for ts_key in (
            "exchange_timestamp",
            "timestamp",
            "received_at",
            "last_trade_time",
        ):
            candidate = parse_dt(x.get(ts_key))

            if candidate is not None:
                dt = candidate
                break

        if dt is None:
            continue

        try:
            price = float(
                x.get("last_price")
                or x.get("ltp")
            )
        except Exception:
            continue

        if price <= 0:
            continue

        ticks[symbol].append((dt, price))


for symbol in ticks:
    ticks[symbol].sort()

print("PARSED SIGNALS =", len(signals))
print("SIGNAL SYMBOLS =", len(needed))
print("TICK SYMBOLS   =", len(ticks))
print(
    "TICK RECORDS   =",
    sum(len(v) for v in ticks.values())
)

missing = sorted(
    symbol
    for symbol in needed
    if symbol not in ticks
)

print("MISSING SIGNAL SYMBOLS =", len(missing))

if missing:
    print("MISSING:", ", ".join(missing[:30]))

print()


def first_price_at_or_after(symbol, dt):
    for t, p in ticks.get(symbol, []):
        if t >= dt:
            return t, p
    return None, None


def floor_candle_time(dt, minutes=3):
    minute = (dt.minute // minutes) * minutes

    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


def build_candles(rows):
    """
    Build OHLC candles from recorded ticks.
    Only last_price is required for this replay.
    """
    buckets = {}

    for dt, price in rows:

        bucket = floor_candle_time(
            dt,
            CANDLE_MINUTES,
        )

        if bucket not in buckets:
            buckets[bucket] = {
                "time": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }

        else:
            c = buckets[bucket]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

    return [
        buckets[k]
        for k in sorted(buckets)
    ]


candles_by_symbol = {
    symbol: build_candles(rows)
    for symbol, rows in ticks.items()
}


def ema(values, period):
    if not values:
        return None

    alpha = 2.0 / (period + 1.0)

    result = values[0]

    for value in values[1:]:
        result = (
            alpha * value
            + (1.0 - alpha) * result
        )

    return result


def atr(candles, period=14):
    if len(candles) < 2:
        return None

    trs = []

    previous_close = candles[0]["close"]

    for c in candles[1:]:

        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - previous_close),
            abs(c["low"] - previous_close),
        )

        trs.append(tr)

        previous_close = c["close"]

    if not trs:
        return None

    # Wilder ATR.
    if len(trs) <= period:
        return sum(trs) / len(trs)

    current = sum(trs[:period]) / period

    for tr in trs[period:]:
        current = (
            (current * (period - 1))
            + tr
        ) / period

    return current


def final_ema_atr_check(symbol, execution_dt, execution_price):
    """
    Use CLOSED candles only.

    A signal at 09:30:52 must not use information from the
    unfinished 09:30-09:33 candle.

    This avoids look-ahead bias.
    """

    current_bucket = floor_candle_time(
        execution_dt,
        CANDLE_MINUTES,
    )

    completed = [
        c
        for c in candles_by_symbol.get(symbol, [])
        if c["time"] < current_bucket
    ]

    if len(completed) < 2:
        return {
            "accepted": False,
            "reason": "INSUFFICIENT_CANDLES",
            "ema9": None,
            "atr": None,
            "distance_atr": None,
        }

    closes = [
        c["close"]
        for c in completed
    ]

    ema9 = ema(
        closes,
        EMA_PERIOD,
    )

    atr_value = atr(
        completed,
        ATR_PERIOD,
    )

    if (
        ema9 is None
        or atr_value is None
        or atr_value <= 0
    ):
        return {
            "accepted": False,
            "reason": "INVALID_ATR",
            "ema9": ema9,
            "atr": atr_value,
            "distance_atr": None,
        }

    distance_atr = (
        abs(execution_price - ema9)
        / atr_value
    )

    return {
        "accepted":
            distance_atr
            <= MAX_EMA_DISTANCE_ATR,

        "reason":
            "PASS"
            if distance_atr
               <= MAX_EMA_DISTANCE_ATR
            else "EMA_DISTANCE",

        "ema9": ema9,
        "atr": atr_value,
        "distance_atr": distance_atr,
    }


def estimate_cost(side, qty, entry, exit_price):
    """
    Use the project's equity costs.py if available.
    """
    try:
        from costs import net_pnl_for_trade

        r = net_pnl_for_trade(
            side,
            qty,
            entry,
            exit_price,
        )

        return (
            float(r.get("costs", 0)),
            float(r.get("net_pnl", 0)),
        )

    except Exception:
        gross = (
            (exit_price - entry) * qty
            if side == "BUY"
            else (entry - exit_price) * qty
        )
        return 0.0, gross


# -------------------------------------------------------
# Build exact position
# -------------------------------------------------------

def create_position(signal):

    entry_time, entry = first_price_at_or_after(
        signal["symbol"],
        signal["dt"],
    )

    if entry is None:
        return None

    quality = final_ema_atr_check(
        signal["symbol"],
        entry_time,
        entry,
    )

    signal["ema_atr_check"] = quality

    if not quality["accepted"]:
        return None

    side = signal["side"]

    if side == "BUY":
        stop = entry * (1.0 - STOP_PCT)
        risk_per_share = entry - stop
        scalp_target = entry + risk_per_share * SCALP_R
        runner_target = entry + risk_per_share * RUNNER_R
    else:
        stop = entry * (1.0 + STOP_PCT)
        risk_per_share = stop - entry
        scalp_target = entry - risk_per_share * SCALP_R
        runner_target = entry - risk_per_share * RUNNER_R

    risk_rupees = CAPITAL * RISK_PCT / 100.0

    qty_risk = int(
        risk_rupees / risk_per_share
    )

    # Existing paper launcher max-position-size = 50% capital.
    qty_capital = int(
        (CAPITAL * 0.50) / entry
    )

    qty = max(
        0,
        min(qty_risk, qty_capital),
    )

    if qty <= 0:
        return None

    scalp_qty = qty // 2
    runner_qty = qty - scalp_qty

    # Ensure at least one share participates.
    if scalp_qty == 0:
        scalp_qty = qty
        runner_qty = 0

    return {
        **signal,
        "entry_time": entry_time,
        "entry": entry,
        "qty": qty,
        "scalp_qty": scalp_qty,
        "runner_qty": runner_qty,
        "stop": stop,
        "t1": scalp_target,
        "t2": runner_target,
        "scalp_done": False,
        "runner_done": runner_qty == 0,
        "legs": [],
        "closed": False,
    }


def close_leg(pos, qty, dt, price, reason):

    if qty <= 0:
        return

    side = pos["side"]

    gross = (
        (price - pos["entry"]) * qty
        if side == "BUY"
        else (pos["entry"] - price) * qty
    )

    costs, net = estimate_cost(
        side,
        qty,
        pos["entry"],
        price,
    )

    pos["legs"].append(
        {
            "dt": dt,
            "qty": qty,
            "exit": price,
            "reason": reason,
            "gross": gross,
            "costs": costs,
            "net": net,
        }
    )


def update_position(pos, dt, price):

    if pos["closed"]:
        return

    side = pos["side"]

    # Hard stop is authoritative.
    stop_hit = (
        price <= pos["stop"]
        if side == "BUY"
        else price >= pos["stop"]
    )

    if stop_hit:

        remaining = 0

        if not pos["scalp_done"]:
            remaining += pos["scalp_qty"]

        if not pos["runner_done"]:
            remaining += pos["runner_qty"]

        close_leg(
            pos,
            remaining,
            dt,
            price,
            "STOP",
        )

        pos["scalp_done"] = True
        pos["runner_done"] = True
        pos["closed"] = True
        return

    # T1
    if not pos["scalp_done"]:

        hit_t1 = (
            price >= pos["t1"]
            if side == "BUY"
            else price <= pos["t1"]
        )

        if hit_t1:

            close_leg(
                pos,
                pos["scalp_qty"],
                dt,
                price,
                "SCALP_1R",
            )

            pos["scalp_done"] = True

            # Existing hybrid concept:
            # remaining runner protected at break-even.
            pos["stop"] = pos["entry"]

    # T2
    if not pos["runner_done"]:

        hit_t2 = (
            price >= pos["t2"]
            if side == "BUY"
            else price <= pos["t2"]
        )

        if hit_t2:

            close_leg(
                pos,
                pos["runner_qty"],
                dt,
                price,
                "RUNNER_2R",
            )

            pos["runner_done"] = True

    if (
        pos["scalp_done"]
        and pos["runner_done"]
    ):
        pos["closed"] = True


# -------------------------------------------------------
# Chronological event replay
# -------------------------------------------------------

open_positions = {}
taken = []
used_symbols = set()

ema_rejections = []

# Merge relevant ticks into timeline.
timeline = []

for symbol, rows in ticks.items():
    for dt, price in rows:
        timeline.append(
            (dt, 1, symbol, price)
        )

# Signal priority before same-timestamp later ticks.
for idx, s in enumerate(signals):
    timeline.append(
        (s["dt"], 0, idx, None)
    )

timeline.sort(
    key=lambda x: (x[0], x[1])
)


for event in timeline:

    dt, kind, key, value = event

    # Stop once seven complete trades have been accepted
    # and all seven are closed.
    if (
        len(taken) >= MAX_TRADES
        and not open_positions
    ):
        break

    if kind == 1:

        symbol = key
        price = value

        pos = open_positions.get(symbol)

        if pos is not None:

            update_position(
                pos,
                dt,
                price,
            )

            if pos["closed"]:
                open_positions.pop(symbol)

        continue

    # Signal event
    s = signals[key]

    if len(taken) >= MAX_TRADES:
        continue

    if s["symbol"] in used_symbols:
        continue

    if len(open_positions) >= MAX_OPEN:
        continue

    pos = create_position(s)

    if pos is None:

        q = s.get("ema_atr_check")

        if q is not None:
            ema_rejections.append(
                {
                    "dt": s["dt"],
                    "symbol": s["symbol"],
                    "side": s["side"],
                    "class": s["class"],
                    "imb": s["imb"],
                    **q,
                }
            )

        continue

    open_positions[pos["symbol"]] = pos
    taken.append(pos)
    used_symbols.add(pos["symbol"])


# -------------------------------------------------------
# Close any still-open positions at last available price
# -------------------------------------------------------

for symbol, pos in list(open_positions.items()):

    rows = ticks.get(symbol, [])

    if not rows:
        continue

    dt, price = rows[-1]

    remaining = 0

    if not pos["scalp_done"]:
        remaining += pos["scalp_qty"]

    if not pos["runner_done"]:
        remaining += pos["runner_qty"]

    close_leg(
        pos,
        remaining,
        dt,
        price,
        "END_OF_DATA",
    )

    pos["closed"] = True


# -------------------------------------------------------
# Results
# -------------------------------------------------------

print("=" * 120)
print("EXECUTED FIRST 7")
print("=" * 120)

total_gross = 0.0
total_costs = 0.0
total_net = 0.0

wins = 0

for i, p in enumerate(taken, 1):

    gross = sum(
        x["gross"]
        for x in p["legs"]
    )

    costs = sum(
        x["costs"]
        for x in p["legs"]
    )

    net = sum(
        x["net"]
        for x in p["legs"]
    )

    total_gross += gross
    total_costs += costs
    total_net += net

    if net > 0:
        wins += 1

    q = p.get(
        "ema_atr_check",
        {}
    )

    ema9 = q.get("ema9")
    atr_value = q.get("atr")
    dist = q.get("distance_atr")

    leg_text = " | ".join(
        f"{x['reason']} "
        f"q={x['qty']} "
        f"@{x['exit']:.2f} "
        f"net={x['net']:+.2f}"
        for x in p["legs"]
    )

    print(
        f"{i}. "
        f"{p['entry_time'].strftime('%H:%M:%S')} "
        f"{p['symbol']:<12} "
        f"{p['side']:<4} "
        f"{p['class']:<7} "
        f"qty={p['qty']:<4} "
        f"entry={p['entry']:.2f} "
        f"EMA9={ema9:.2f} "
        f"ATR={atr_value:.4f} "
        f"dist={dist:.3f}ATR "
        f"SL={p['stop']:.2f} "
        f"T1={p['t1']:.2f} "
        f"T2={p['t2']:.2f} "
        f"| {leg_text} "
        f"| TOTAL={net:+.2f}"
    )


print()

print("=" * 120)
print("EMA9 / ATR REJECTIONS BEFORE FIRST 7 EXECUTIONS")
print("=" * 120)

for r in ema_rejections:

    d = r.get("distance_atr")
    e = r.get("ema9")
    a = r.get("atr")

    d_text = (
        f"{d:.3f}"
        if d is not None
        else "NA"
    )

    e_text = (
        f"{e:.2f}"
        if e is not None
        else "NA"
    )

    a_text = (
        f"{a:.4f}"
        if a is not None
        else "NA"
    )

    print(
        r["dt"].strftime("%H:%M:%S"),
        f"{r['symbol']:<12}",
        f"{r['side']:<4}",
        f"{r['class']:<7}",
        f"EMA9={e_text}",
        f"ATR={a_text}",
        f"DIST={d_text}",
        f"RESULT={r['reason']}",
    )

print()
print(
    "TOTAL EMA/ATR REJECTIONS =",
    len(ema_rejections)
)

print()
print("=" * 120)
print("₹5,000 STRATEGY RESULT")
print("=" * 120)

print("Trades          :", len(taken))
print("Wins            :", wins)
print("Losses          :", len(taken) - wins)
print(
    "Win rate        :",
    f"{wins / len(taken) * 100:.2f}%"
    if taken else "0.00%",
)

print(f"Gross P&L       : ₹{total_gross:+.2f}")
print(f"Costs           : ₹{total_costs:.2f}")
print(f"NET P&L         : ₹{total_net:+.2f}")
print(f"Starting capital: ₹{CAPITAL:,.2f}")
print(
    "Return          :",
    f"{total_net / CAPITAL * 100:+.2f}%"
)

print("=" * 120)
