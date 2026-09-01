#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime, time, timedelta
from collections import defaultdict, deque
from statistics import median

DATE = "2026-08-27"

TICK_FILE = Path(
    f"runtime/equity_socket_shadow/"
    f"ticks_{DATE}_recovered.jsonl"
)

# -------------------------------------------------------
# STRATEGY
# -------------------------------------------------------

START = time(9, 15)
END   = time(11, 47)

WINDOW_SECONDS = 60
MIN_SAMPLES = 10

STRONG = 0.20
EXTREME = 0.60
PERSISTENCE = 0.70

# Avoid triggering from only a few seconds of data.
MIN_CONFIRMATION_SECONDS = 30

FORWARD_MINUTES = [1, 3, 5, 10, 15, 30, 60]


def parse_dt(v):
    if not v:
        return None

    try:
        return datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        return None


def get_symbol(x):
    for k in ("symbol", "tradingsymbol"):
        if x.get(k):
            return str(x[k]).upper()

    inst = x.get("instrument")
    if isinstance(inst, dict):
        return str(
            inst.get("tradingsymbol")
            or inst.get("symbol")
            or ""
        ).upper()

    return ""


def get_price(x):
    for k in (
        "last_price",
        "ltp",
        "price",
        "last_traded_price",
    ):
        try:
            v = float(x.get(k))
            if v > 0:
                return v
        except Exception:
            pass

    return None


def extract_depth(x):
    # First try already-calculated quantities.
    try:
        bq = float(x.get("bid_qty"))
        aq = float(x.get("ask_qty"))

        if bq >= 0 and aq >= 0 and (bq + aq) > 0:
            return bq, aq
    except Exception:
        pass

    depth = x.get("depth") or {}

    if not isinstance(depth, dict):
        return None

    bids = depth.get("buy") or depth.get("bids") or []
    asks = depth.get("sell") or depth.get("asks") or []

    def qty(rows):
        total = 0.0

        for row in rows[:5]:
            if not isinstance(row, dict):
                continue

            try:
                total += max(
                    0.0,
                    float(
                        row.get("quantity")
                        or row.get("qty")
                        or 0
                    ),
                )
            except Exception:
                pass

        return total

    bq = qty(bids)
    aq = qty(asks)

    if bq + aq <= 0:
        return None

    return bq, aq


def imbalance(bid_qty, ask_qty):
    total = bid_qty + ask_qty

    if total <= 0:
        return None

    return (bid_qty - ask_qty) / total


print("=" * 115)
print("OPENING DEPTH STRATEGY — CHRONOLOGICAL BACKTEST")
print("=" * 115)
print()
print("Window              :", WINDOW_SECONDS, "seconds")
print("Strong threshold    :", STRONG)
print("Extreme threshold   :", EXTREME)
print("Persistence         :", PERSISTENCE)
print("Minimum confirmation:", MIN_CONFIRMATION_SECONDS, "seconds")
print()

ticks = defaultdict(list)

with TICK_FILE.open(
    "r",
    encoding="utf-8",
    errors="replace",
) as f:

    for line in f:

        try:
            x = json.loads(line)
        except Exception:
            continue

        dt = parse_dt(
            x.get("timestamp")
            or x.get("exchange_timestamp")
            or x.get("ts")
        )

        if dt is None:
            continue

        if not (START <= dt.time() <= END):
            continue

        symbol = get_symbol(x)
        price = get_price(x)
        depth = extract_depth(x)

        if not symbol or price is None or depth is None:
            continue

        bq, aq = depth
        imb = imbalance(bq, aq)

        if imb is None:
            continue

        ticks[symbol].append(
            {
                "dt": dt,
                "price": price,
                "imb": imb,
                "bid_qty": bq,
                "ask_qty": aq,
            }
        )


for symbol in ticks:
    ticks[symbol].sort(key=lambda z: z["dt"])


print("SYMBOLS WITH DATA =", len(ticks))
print()


signals = []


# -------------------------------------------------------
# WALK FORWARD THROUGH EVERY STOCK
# -------------------------------------------------------

for symbol, rows in ticks.items():

    window = deque()
    first_dt = rows[0]["dt"]

    for i, row in enumerate(rows):

        now = row["dt"]

        window.append(row)

        cutoff = now - timedelta(
            seconds=WINDOW_SECONDS
        )

        while window and window[0]["dt"] < cutoff:
            window.popleft()

        if len(window) < MIN_SAMPLES:
            continue

        coverage = (
            window[-1]["dt"] -
            window[0]["dt"]
        ).total_seconds()

        # Require actual time coverage.
        if coverage < MIN_CONFIRMATION_SECONDS:
            continue

        values = [r["imb"] for r in window]

        med = median(values)

        buy_fraction = (
            sum(v >= STRONG for v in values)
            / len(values)
        )

        sell_fraction = (
            sum(v <= -STRONG for v in values)
            / len(values)
        )

        side = None
        persistence = 0.0

        if (
            med >= STRONG
            and buy_fraction >= PERSISTENCE
        ):
            side = "BUY"
            persistence = buy_fraction

        elif (
            med <= -STRONG
            and sell_fraction >= PERSISTENCE
        ):
            side = "SELL"
            persistence = sell_fraction

        if side is None:
            continue

        classification = (
            "EXTREME"
            if abs(med) >= EXTREME
            else "STRONG"
        )

        entry_price = row["price"]

        # -----------------------------------------------
        # Forward returns
        # -----------------------------------------------

        future = {}

        for mins in FORWARD_MINUTES:

            target = now + timedelta(minutes=mins)

            candidate = None

            for later in rows[i:]:
                if later["dt"] >= target:
                    candidate = later
                    break

            if candidate is None:
                future[mins] = None
                continue

            raw_pct = (
                candidate["price"] - entry_price
            ) / entry_price * 100.0

            directional_pct = (
                raw_pct
                if side == "BUY"
                else -raw_pct
            )

            future[mins] = directional_pct

        # -----------------------------------------------
        # MFE / MAE for next 30 minutes
        # -----------------------------------------------

        horizon = now + timedelta(minutes=30)

        future_rows = [
            r
            for r in rows[i:]
            if r["dt"] <= horizon
        ]

        directional_moves = []

        for r in future_rows:

            raw = (
                r["price"] - entry_price
            ) / entry_price * 100.0

            directional_moves.append(
                raw if side == "BUY" else -raw
            )

        mfe = (
            max(directional_moves)
            if directional_moves
            else 0.0
        )

        mae = (
            min(directional_moves)
            if directional_moves
            else 0.0
        )

        signals.append(
            {
                "symbol": symbol,
                "time": now,
                "side": side,
                "class": classification,
                "median": med,
                "persistence": persistence,
                "entry": entry_price,
                "future": future,
                "mfe": mfe,
                "mae": mae,
                "samples": len(window),
                "coverage": coverage,
            }
        )

        # ONE TRADE PER SYMBOL.
        break


signals.sort(key=lambda x: x["time"])


print("=" * 115)
print("EARLIEST VALID SIGNAL PER STOCK")
print("=" * 115)

header = (
    f"{'TIME':<9}"
    f"{'SYMBOL':<14}"
    f"{'SIDE':<7}"
    f"{'CLASS':<10}"
    f"{'IMB':>8}"
    f"{'PERS':>8}"
    f"{'ENTRY':>11}"
    f"{'1M':>9}"
    f"{'3M':>9}"
    f"{'5M':>9}"
    f"{'10M':>9}"
    f"{'15M':>9}"
    f"{'30M':>9}"
    f"{'MFE':>9}"
    f"{'MAE':>9}"
)

print(header)
print("-" * len(header))


def fp(v):
    if v is None:
        return "       -"

    return f"{v:+8.3f}%"


for s in signals:

    f = s["future"]

    print(
        f"{s['time'].strftime('%H:%M:%S'):<9}"
        f"{s['symbol']:<14}"
        f"{s['side']:<7}"
        f"{s['class']:<10}"
        f"{s['median']:>8.3f}"
        f"{s['persistence']*100:>7.1f}%"
        f"{s['entry']:>11.2f}"
        f"{fp(f[1])}"
        f"{fp(f[3])}"
        f"{fp(f[5])}"
        f"{fp(f[10])}"
        f"{fp(f[15])}"
        f"{fp(f[30])}"
        f"{s['mfe']:>+8.3f}%"
        f"{s['mae']:>+8.3f}%"
    )


print()
print("=" * 115)
print("SUMMARY")
print("=" * 115)

print("TOTAL SIGNALS =", len(signals))

buys = [s for s in signals if s["side"] == "BUY"]
sells = [s for s in signals if s["side"] == "SELL"]

print("BUY SIGNALS   =", len(buys))
print("SELL SIGNALS  =", len(sells))

print()


for mins in [1, 3, 5, 10, 15, 30]:

    vals = [
        s["future"][mins]
        for s in signals
        if s["future"][mins] is not None
    ]

    if not vals:
        continue

    wins = sum(v > 0 for v in vals)

    avg = sum(vals) / len(vals)

    print(
        f"{mins:>2} MIN | "
        f"N={len(vals):>3} "
        f"W={wins:>3} "
        f"WR={wins/len(vals)*100:>6.2f}% "
        f"AVG_DIRECTIONAL_MOVE={avg:+.4f}%"
    )


print()
print("=" * 115)
print("EXTREME vs STRONG")
print("=" * 115)

for classification in ["EXTREME", "STRONG"]:

    group = [
        s for s in signals
        if s["class"] == classification
    ]

    if not group:
        continue

    vals = [
        s["future"][15]
        for s in group
        if s["future"][15] is not None
    ]

    if not vals:
        continue

    wins = sum(v > 0 for v in vals)

    print(
        classification,
        "| N=", len(vals),
        "| 15M WR=",
        f"{wins/len(vals)*100:.2f}%",
        "| AVG=",
        f"{sum(vals)/len(vals):+.4f}%"
    )


print()
print("=" * 115)
print("FIRST SIGNAL TIME DISTRIBUTION")
print("=" * 115)

buckets = defaultdict(int)

for s in signals:

    t = s["time"].time()

    if t < time(9, 16):
        bucket = "09:15-09:16"

    elif t < time(9, 17):
        bucket = "09:16-09:17"

    elif t < time(9, 20):
        bucket = "09:17-09:20"

    elif t < time(9, 30):
        bucket = "09:20-09:30"

    else:
        bucket = "AFTER 09:30"

    buckets[bucket] += 1


for bucket in [
    "09:15-09:16",
    "09:16-09:17",
    "09:17-09:20",
    "09:20-09:30",
    "AFTER 09:30",
]:
    print(
        f"{bucket:<15} = "
        f"{buckets.get(bucket, 0)}"
    )


print()
print("=" * 115)
print("IMPORTANT")
print("=" * 115)

print(
    "Every signal above uses ONLY depth observations "
    "available BEFORE its entry timestamp."
)

print(
    "No 11:37-11:47 classification was used to "
    "decide a 09:15 trade."
)

print(
    "Therefore this is a chronological test of the "
    "opening-depth hypothesis, not a look-ahead replay."
)
