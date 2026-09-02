#!/usr/bin/env python3

import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd

import config
from kiteconnect import KiteConnect

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
MAX_TRADES = 100

STOP_PCT = 0.45 / 100.0

MAX_EMA_DISTANCE_ATR = 0.75
EMA_PERIOD = 9
ATR_PERIOD = 14

SCALP_R = 1.0
RUNNER_R = 2.0


# ============================================================
# AUTH
# ============================================================

token_file = Path(
    getattr(config, "ACCESS_TOKEN_FILE", "access_token.txt")
)

access_token = token_file.read_text().strip()

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(access_token)


# ============================================================
# HELPERS
# ============================================================

def parse_dt(v):
    if not v:
        return None

    try:
        return datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        return None


def ema(series, period):
    s = pd.Series(series, dtype=float)

    return float(
        s.ewm(
            span=period,
            adjust=False
        ).mean().iloc[-1]
    )


def atr_from_df(df, period=14):

    if len(df) < 2:
        return None

    high = pd.to_numeric(df["high"])
    low = pd.to_numeric(df["low"])
    close = pd.to_numeric(df["close"])

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    tr = tr.dropna()

    if len(tr) < period:
        return float(tr.mean())

    # Wilder-style ATR
    atr_value = float(
        tr.iloc[:period].mean()
    )

    for value in tr.iloc[period:]:
        atr_value = (
            atr_value * (period - 1)
            + float(value)
        ) / period

    return atr_value


# ============================================================
# PARSE DEPTH SIGNALS
# ============================================================

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

    signals.append(
        {
            "dt": datetime.fromisoformat(
                f"{DATE}T{m.group(1)}"
            ),
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
        -abs(x["imb"]),
        -x["pers"],
        x["symbol"],
    )
)


# ============================================================
# INSTRUMENT TOKEN MAP
# ============================================================

print("Loading NSE instruments...")

instruments = kite.instruments("NSE")

token_map = {}

for row in instruments:

    symbol = row.get("tradingsymbol")

    if symbol:
        token_map[symbol] = row.get(
            "instrument_token"
        )

print(
    "Instrument tokens loaded:",
    len(token_map)
)


# ============================================================
# HISTORICAL EMA / ATR CACHE
# ============================================================

indicator_cache = {}


def get_historical_ema_atr(
    symbol,
    signal_dt,
    execution_price,
):

    key = (
        symbol,
        signal_dt.strftime(
            "%Y-%m-%d %H:%M"
        ),
    )

    if key in indicator_cache:
        return indicator_cache[key]

    token = token_map.get(symbol)

    if not token:

        result = {
            "accepted": False,
            "reason": "TOKEN_NOT_FOUND",
            "ema9": None,
            "atr": None,
            "distance_atr": None,
        }

        indicator_cache[key] = result
        return result

    # We need enough candles BEFORE signal time.
    from_dt = signal_dt - timedelta(
        days=2
    )

    # Critical:
    # Don't include unfinished signal candle.
    #
    # For 09:30:52 on 3-minute timeframe,
    # latest usable completed candle ends at 09:30.
    minute = (
        signal_dt.minute // 3
    ) * 3

    current_bucket = signal_dt.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )

    to_dt = current_bucket

    try:

        candles = kite.historical_data(
            token,
            from_dt,
            to_dt,
            "3minute",
            continuous=False,
            oi=False,
        )

    except Exception as e:

        result = {
            "accepted": False,
            "reason":
                f"HISTORICAL_ERROR:{e}",
            "ema9": None,
            "atr": None,
            "distance_atr": None,
        }

        indicator_cache[key] = result

        time.sleep(0.4)

        return result

    time.sleep(0.35)

    if not candles:

        result = {
            "accepted": False,
            "reason": "NO_HISTORICAL_CANDLES",
            "ema9": None,
            "atr": None,
            "distance_atr": None,
        }

        indicator_cache[key] = result
        return result

    df = pd.DataFrame(candles)

    # Normalize timestamps
    df["date"] = pd.to_datetime(
        df["date"]
    ).dt.tz_localize(None)

    # Ensure absolutely no future candle
    # leaks into the signal calculation.
    df = df[
        df["date"] < current_bucket
    ].copy()

    if len(df) < 14:

        result = {
            "accepted": False,
            "reason":
                f"INSUFFICIENT_HISTORICAL_CANDLES:{len(df)}",
            "ema9": None,
            "atr": None,
            "distance_atr": None,
        }

        indicator_cache[key] = result
        return result

    ema9 = ema(
        df["close"].tolist(),
        EMA_PERIOD,
    )

    atr_value = atr_from_df(
        df,
        ATR_PERIOD,
    )

    if (
        atr_value is None
        or atr_value <= 0
    ):

        result = {
            "accepted": False,
            "reason": "INVALID_ATR",
            "ema9": ema9,
            "atr": atr_value,
            "distance_atr": None,
        }

        indicator_cache[key] = result
        return result

    distance_atr = (
        abs(
            execution_price
            - ema9
        )
        / atr_value
    )

    result = {
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

    indicator_cache[key] = result

    return result


# ============================================================
# LOAD RAW TICKS
# ============================================================

needed = {
    s["symbol"]
    for s in signals
}

ticks = defaultdict(list)

print("Loading recovered ticks...")

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

        for field in (
            "exchange_timestamp",
            "timestamp",
            "received_at",
            "last_trade_time",
        ):

            candidate = parse_dt(
                x.get(field)
            )

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

        ticks[symbol].append(
            (dt, price)
        )


for symbol in ticks:
    ticks[symbol].sort()


print(
    "Signals:",
    len(signals)
)

print(
    "Tick symbols:",
    len(ticks)
)

print(
    "Tick records:",
    sum(
        len(v)
        for v in ticks.values()
    )
)


def first_price_at_or_after(
    symbol,
    dt,
):

    for t, p in ticks.get(
        symbol,
        []
    ):

        if t >= dt:
            return t, p

    return None, None


# ============================================================
# COSTS
# ============================================================

def estimate_cost(
    side,
    qty,
    entry,
    exit_price,
):

    try:

        from costs import (
            net_pnl_for_trade
        )

        r = net_pnl_for_trade(
            side,
            qty,
            entry,
            exit_price,
        )

        return (
            float(
                r.get(
                    "costs",
                    0
                )
            ),
            float(
                r.get(
                    "net_pnl",
                    0
                )
            ),
        )

    except Exception:

        gross = (
            (exit_price - entry)
            * qty
            if side == "BUY"
            else
            (entry - exit_price)
            * qty
        )

        return 0.0, gross


# ============================================================
# POSITION CREATION
# ============================================================

def create_position(signal):

    entry_time, entry = (
        first_price_at_or_after(
            signal["symbol"],
            signal["dt"],
        )
    )

    if entry is None:
        return None

    quality = (
        get_historical_ema_atr(
            signal["symbol"],
            entry_time,
            entry,
        )
    )

    signal["ema_atr_check"] = (
        quality
    )

    if not quality["accepted"]:
        return None

    side = signal["side"]

    if side == "BUY":

        stop = (
            entry
            * (1 - STOP_PCT)
        )

        risk_per_share = (
            entry - stop
        )

        t1 = (
            entry
            + risk_per_share
            * SCALP_R
        )

        t2 = (
            entry
            + risk_per_share
            * RUNNER_R
        )

    else:

        stop = (
            entry
            * (1 + STOP_PCT)
        )

        risk_per_share = (
            stop - entry
        )

        t1 = (
            entry
            - risk_per_share
            * SCALP_R
        )

        t2 = (
            entry
            - risk_per_share
            * RUNNER_R
        )

    risk_rupees = (
        CAPITAL
        * RISK_PCT
        / 100
    )

    qty_risk = int(
        risk_rupees
        / risk_per_share
    )

    # Paper launcher:
    # max position size = 50% capital
    qty_capital = int(
        (
            CAPITAL * 0.50
        ) / entry
    )

    qty = max(
        0,
        min(
            qty_risk,
            qty_capital
        )
    )

    if qty <= 0:
        return None

    scalp_qty = qty // 2
    runner_qty = (
        qty - scalp_qty
    )

    if scalp_qty <= 0:
        scalp_qty = qty
        runner_qty = 0

    return {
        **signal,

        "entry_time":
            entry_time,

        "entry":
            entry,

        "qty":
            qty,

        "scalp_qty":
            scalp_qty,

        "runner_qty":
            runner_qty,

        "stop":
            stop,

        "initial_stop":
            stop,

        "t1":
            t1,

        "t2":
            t2,

        "scalp_done":
            False,

        "runner_done":
            runner_qty == 0,

        "legs":
            [],

        "closed":
            False,
    }


# ============================================================
# EXIT LOGIC
# ============================================================

def close_leg(
    pos,
    qty,
    dt,
    price,
    reason,
):

    if qty <= 0:
        return

    if pos["side"] == "BUY":

        gross = (
            price
            - pos["entry"]
        ) * qty

    else:

        gross = (
            pos["entry"]
            - price
        ) * qty

    costs, net = (
        estimate_cost(
            pos["side"],
            qty,
            pos["entry"],
            price,
        )
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


def update_position(
    pos,
    dt,
    price,
):

    if pos["closed"]:
        return

    side = pos["side"]

    stop_hit = (
        price <= pos["stop"]
        if side == "BUY"
        else
        price >= pos["stop"]
    )

    if stop_hit:

        remaining = 0

        if not pos[
            "scalp_done"
        ]:
            remaining += (
                pos[
                    "scalp_qty"
                ]
            )

        if not pos[
            "runner_done"
        ]:
            remaining += (
                pos[
                    "runner_qty"
                ]
            )

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

    if not pos["scalp_done"]:

        hit_t1 = (
            price >= pos["t1"]
            if side == "BUY"
            else
            price <= pos["t1"]
        )

        if hit_t1:

            close_leg(
                pos,
                pos["scalp_qty"],
                dt,
                price,
                "SCALP_1R",
            )

            pos["scalp_done"] = (
                True
            )

            # runner -> break-even
            pos["stop"] = (
                pos["entry"]
            )

    if not pos["runner_done"]:

        hit_t2 = (
            price >= pos["t2"]
            if side == "BUY"
            else
            price <= pos["t2"]
        )

        if hit_t2:

            close_leg(
                pos,
                pos["runner_qty"],
                dt,
                price,
                "RUNNER_2R",
            )

            pos["runner_done"] = (
                True
            )

    if (
        pos["scalp_done"]
        and pos["runner_done"]
    ):
        pos["closed"] = True


# ============================================================
# EVENT REPLAY
# ============================================================

open_positions = {}
taken = []
used_symbols = set()

ema_rejections = []


timeline = []

for symbol, rows in ticks.items():

    for dt, price in rows:

        timeline.append(
            (
                dt,
                1,
                symbol,
                price
            )
        )


for idx, s in enumerate(signals):

    timeline.append(
        (
            s["dt"],
            0,
            idx,
            None
        )
    )


timeline.sort(
    key=lambda x: (
        x[0],
        x[1]
    )
)


for event in timeline:

    dt, kind, key, value = (
        event
    )

    if (
        len(taken)
        >= MAX_TRADES
        and not open_positions
    ):
        break

    # -----------------------
    # PRICE EVENT
    # -----------------------

    if kind == 1:

        symbol = key
        price = value

        pos = open_positions.get(
            symbol
        )

        if pos:

            update_position(
                pos,
                dt,
                price,
            )

            if pos["closed"]:

                open_positions.pop(
                    symbol
                )

        continue

    # -----------------------
    # SIGNAL EVENT
    # -----------------------

    s = signals[key]

    if len(taken) >= MAX_TRADES:
        continue

    if (
        s["symbol"]
        in used_symbols
    ):
        continue

    if (
        len(open_positions)
        >= MAX_OPEN
    ):
        continue

    pos = create_position(s)

    if pos is None:

        q = s.get(
            "ema_atr_check"
        )

        if q:

            ema_rejections.append(
                {
                    "dt":
                        s["dt"],

                    "symbol":
                        s["symbol"],

                    "side":
                        s["side"],

                    "class":
                        s["class"],

                    "imb":
                        s["imb"],

                    **q,
                }
            )

        continue

    open_positions[
        pos["symbol"]
    ] = pos

    taken.append(pos)

    used_symbols.add(
        pos["symbol"]
    )


# ============================================================
# CLOSE REMAINING AT LAST RECORDED PRICE
# ============================================================

for symbol, pos in list(
    open_positions.items()
):

    rows = ticks.get(
        symbol,
        []
    )

    if not rows:
        continue

    dt, price = rows[-1]

    remaining = 0

    if not pos["scalp_done"]:
        remaining += (
            pos["scalp_qty"]
        )

    if not pos["runner_done"]:
        remaining += (
            pos["runner_qty"]
        )

    close_leg(
        pos,
        remaining,
        dt,
        price,
        "END_OF_DATA",
    )

    pos["closed"] = True


# ============================================================
# OUTPUT
# ============================================================

print()
print("=" * 125)
print("ALL QUALIFYING TRADES — MAXIMUM 100")
print("=" * 125)

total_gross = 0.0
total_costs = 0.0
total_net = 0.0
wins = 0


for i, p in enumerate(
    taken,
    1
):

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

    q = p["ema_atr_check"]

    legs = " | ".join(
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
        f"EMA9={q['ema9']:.2f} "
        f"ATR={q['atr']:.4f} "
        f"dist={q['distance_atr']:.3f}ATR "
        f"SL={p['initial_stop']:.2f} "
        f"T1={p['t1']:.2f} "
        f"T2={p['t2']:.2f} "
        f"| {legs} "
        f"| TOTAL={net:+.2f}"
    )


print()
print("=" * 125)
print("FIRST EMA/ATR REJECTIONS")
print("=" * 125)

for r in ema_rejections[:40]:

    dist = r.get(
        "distance_atr"
    )

    print(
        r["dt"].strftime(
            "%H:%M:%S"
        ),
        f"{r['symbol']:<12}",
        f"{r['side']:<4}",
        f"{r['class']:<7}",
        "DIST=",
        (
            f"{dist:.3f}"
            if dist is not None
            else "NA"
        ),
        "RESULT=",
        r["reason"],
    )


print()
print("=" * 125)
print("₹5,000 FINAL RESULT")
print("=" * 125)

print(
    "Trades          :",
    len(taken)
)

print(
    "Wins            :",
    wins
)

print(
    "Losses          :",
    len(taken) - wins
)

print(
    "Win rate        :",
    (
        f"{wins / len(taken) * 100:.2f}%"
        if taken
        else "0.00%"
    )
)

print(
    f"Gross P&L       : ₹{total_gross:+.2f}"
)

print(
    f"Costs           : ₹{total_costs:.2f}"
)

print(
    f"NET P&L         : ₹{total_net:+.2f}"
)

print(
    f"Starting capital: ₹{CAPITAL:,.2f}"
)

print(
    "Return          :",
    f"{total_net / CAPITAL * 100:+.2f}%"
)

print("=" * 125)

