#!/usr/bin/env python3

import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd

import config
from kiteconnect import KiteConnect

DATE = "2026-08-27"
TRADE_FILE = Path("trade_history.jsonl")
INTERVAL = "3minute"
ADX_PERIOD = 14

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def parse_dt(v):
    if not v:
        return None

    s = str(v).replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fmt(v, n=2):
    if v is None:
        return "NA"
    try:
        return f"{float(v):.{n}f}"
    except Exception:
        return str(v)


# ------------------------------------------------------------
# Wilder ADX / +DI / -DI
# ------------------------------------------------------------

def calculate_adx(df, period=14):
    df = df.copy()

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)

    plus_dm[(up_move > down_move) & (up_move > 0)] = \
        up_move[(up_move > down_move) & (up_move > 0)]

    minus_dm[(down_move > up_move) & (down_move > 0)] = \
        down_move[(down_move > up_move) & (down_move > 0)]

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    # Wilder smoothing = alpha 1/period.
    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    plus_smoothed = plus_dm.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    minus_smoothed = minus_dm.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    plus_di = 100 * plus_smoothed / atr
    minus_di = 100 * minus_smoothed / atr

    denom = plus_di + minus_di

    dx = (
        100 *
        (plus_di - minus_di).abs() /
        denom.replace(0, float("nan"))
    )

    adx = dx.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    df["adx"] = adx

    return df


def classify(values):
    """
    values are chronological, oldest -> newest.

    ALL_RISING  : every step increased
    ALL_FALLING : every step decreased
    MOSTLY_RISING/FALLING : majority of steps same direction
    FLAT_MIXED  : no useful dominance
    """

    values = [
        float(v)
        for v in values
        if pd.notna(v)
    ]

    if len(values) < 2:
        return "NA"

    diffs = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    eps = 0.01

    rising = sum(d > eps for d in diffs)
    falling = sum(d < -eps for d in diffs)
    flat = len(diffs) - rising - falling

    if rising == len(diffs):
        return "ALL_RISING"

    if falling == len(diffs):
        return "ALL_FALLING"

    if rising > falling:
        return "MOSTLY_RISING"

    if falling > rising:
        return "MOSTLY_FALLING"

    return "FLAT_MIXED"


# ------------------------------------------------------------
# Load actual trades
# ------------------------------------------------------------

if not TRADE_FILE.exists():
    raise SystemExit(
        f"Missing {TRADE_FILE}"
    )

raw = []

with TRADE_FILE.open("r", errors="replace") as f:
    for line in f:
        try:
            x = json.loads(line)
        except Exception:
            continue

        if x.get("date") != DATE:
            continue

        if not x.get("symbol"):
            continue

        entry_time = parse_dt(
            x.get("entry_time")
        )

        if entry_time is None:
            continue

        raw.append(x)

if not raw:
    raise SystemExit(
        "No trades found for requested date"
    )

# ------------------------------------------------------------
# Combine scalp + runner legs belonging to same entry signal
# ------------------------------------------------------------

groups = {}

for x in raw:
    key = (
        x.get("signal_id")
        or x.get("entry_operation_id")
        or (
            x.get("symbol"),
            x.get("direction"),
            x.get("entry_time"),
            x.get("entry"),
        )
    )

    if key not in groups:
        groups[key] = {
            "symbol": x.get("symbol"),
            "direction": x.get("direction"),
            "entry_time": parse_dt(
                x.get("entry_time")
            ),
            "signal_candle_close": parse_dt(
                x.get("signal_candle_close")
            ),
            "entry": float(
                x.get("entry") or 0
            ),
            "pnl": 0.0,
            "legs": 0,
            "results": [],
            "signal_id": x.get("signal_id"),
        }

    groups[key]["pnl"] += float(
        x.get("pnl") or 0
    )

    groups[key]["legs"] += 1

    if x.get("result"):
        groups[key]["results"].append(
            str(x.get("result"))
        )

trades = list(groups.values())

trades.sort(
    key=lambda x: x["entry_time"]
)

print("=" * 150)
print("ALL AUGUST 27 TRADE ENTRIES — ADX TREND ANALYSIS")
print("=" * 150)
print("Raw exit legs :", len(raw))
print("Unique entries:", len(trades))
print()

# ------------------------------------------------------------
# Connect Kite
# ------------------------------------------------------------

kite = KiteConnect(
    api_key=config.API_KEY
)

token_file = Path(
    getattr(
        config,
        "ACCESS_TOKEN_FILE",
        "access_token.txt"
    )
)

if token_file.exists():
    access_token = token_file.read_text().strip()
else:
    access_token = getattr(
        config,
        "ACCESS_TOKEN",
        None
    )

if not access_token:
    raise SystemExit(
        "No Kite access token available"
    )

kite.set_access_token(access_token)

print("Loading NSE instruments...")

instruments = kite.instruments("NSE")

token_map = {}

for row in instruments:
    symbol = row.get("tradingsymbol")
    token = row.get("instrument_token")

    if symbol and token:
        token_map[symbol] = token

print(
    "Instrument tokens:",
    len(token_map)
)

# ------------------------------------------------------------
# Download enough history for every symbol
# ------------------------------------------------------------

symbols = sorted(
    {t["symbol"] for t in trades}
)

history = {}

for i, symbol in enumerate(symbols, 1):

    token = token_map.get(symbol)

    if token is None:
        print(
            f"WARNING: no token for {symbol}"
        )
        continue

    # Fetch previous trading history so ADX(14)
    # is already properly warmed up.
    start = datetime.fromisoformat(
        f"{DATE}T09:00:00+05:30"
    ) - timedelta(days=5)

    end = datetime.fromisoformat(
        f"{DATE}T15:30:00+05:30"
    )

    try:
        candles = kite.historical_data(
            token,
            start,
            end,
            INTERVAL,
            continuous=False,
            oi=False,
        )

    except Exception as e:
        print(
            f"ERROR {symbol}: {e}"
        )
        continue

    if not candles:
        continue

    df = pd.DataFrame(candles)

    df["date"] = pd.to_datetime(
        df["date"]
    )

    df = calculate_adx(
        df,
        ADX_PERIOD
    )

    history[symbol] = df

    print(
        f"[{i:02d}/{len(symbols):02d}] "
        f"{symbol:<12} candles={len(df)}"
    )

    time.sleep(0.25)

# ------------------------------------------------------------
# Analyze each actual entry
# ------------------------------------------------------------

results = []

print()
print("=" * 150)
print("TRADE-BY-TRADE ADX ANALYSIS")
print("=" * 150)

for n, trade in enumerate(trades, 1):

    symbol = trade["symbol"]
    direction = trade["direction"]
    entry_time = trade["entry_time"]
    signal_close = (
        trade["signal_candle_close"]
        or entry_time
    )

    df = history.get(symbol)

    if df is None or df.empty:
        print(
            f"{n}. {symbol}: NO HISTORY"
        )
        continue

    # Only candles that were completed by the signal close.
    eligible = df[
        df["date"] < signal_close
    ].copy()

    eligible = eligible[
        eligible["adx"].notna()
    ]

    if len(eligible) < 6:
        print(
            f"{n}. {symbol}: "
            "INSUFFICIENT ADX HISTORY"
        )
        continue

    last = eligible.iloc[-1]

    adx = float(last["adx"])
    plus_di = float(last["plus_di"])
    minus_di = float(last["minus_di"])

    # Window N means N ADX observations.
    # For example 5 observations cover 4 candle-to-candle changes.
    classifications = {}

    sequences = {}

    for window in (2, 3, 4, 5):
        vals = (
            eligible["adx"]
            .tail(window)
            .tolist()
        )

        classifications[window] = \
            classify(vals)

        sequences[window] = vals

    di_direction = (
        "BUY"
        if plus_di > minus_di
        else "SELL"
        if minus_di > plus_di
        else "NEUTRAL"
    )

    di_matches = (
        di_direction == direction
    )

    pnl = trade["pnl"]

    result = {
        **trade,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "di_direction": di_direction,
        "di_matches": di_matches,
        "adx2": classifications[2],
        "adx3": classifications[3],
        "adx4": classifications[4],
        "adx5": classifications[5],
        "seq5": sequences[5],
    }

    results.append(result)

    seq = " -> ".join(
        f"{v:.2f}"
        for v in sequences[5]
    )

    print()
    print(
        f"{n:02d}. {symbol:<12} "
        f"{direction:<4} "
        f"entry={trade['entry']:.2f} "
        f"PnL=₹{pnl:+.2f}"
    )

    print(
        f"    ADX={adx:.2f} "
        f"+DI={plus_di:.2f} "
        f"-DI={minus_di:.2f} "
        f"DI_SIDE={di_direction} "
        f"DI_MATCH={di_matches}"
    )

    print(
        f"    Last 5 ADX: {seq}"
    )

    print(
        f"    2={classifications[2]:<15} "
        f"3={classifications[3]:<15} "
        f"4={classifications[4]:<15} "
        f"5={classifications[5]:<15}"
    )

# ------------------------------------------------------------
# Strategy simulations
# ------------------------------------------------------------

def summarize(name, selector):
    chosen = [
        r for r in results
        if selector(r)
    ]

    wins = sum(
        r["pnl"] > 0
        for r in chosen
    )

    losses = sum(
        r["pnl"] < 0
        for r in chosen
    )

    net = sum(
        r["pnl"]
        for r in chosen
    )

    rejected = len(results) - len(chosen)

    win_rate = (
        wins / len(chosen) * 100
        if chosen
        else 0
    )

    return {
        "name": name,
        "trades": len(chosen),
        "rejected": rejected,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net": net,
    }


tests = []

tests.append(
    summarize(
        "BASELINE ALL",
        lambda r: True
    )
)

# Your proposed concept:
# reject BUY/SELL if ADX has been falling.
for window in (2, 3, 4, 5):

    tests.append(
        summarize(
            f"REJECT ALL_FALLING {window}",
            lambda r, w=window:
                r[f"adx{w}"] != "ALL_FALLING"
        )
    )

# Only allow sustained ADX improvement.
for window in (2, 3, 4, 5):

    tests.append(
        summarize(
            f"ONLY ALL_RISING {window}",
            lambda r, w=window:
                r[f"adx{w}"] == "ALL_RISING"
        )
    )

# Directional ADX tests.
tests.append(
    summarize(
        "ADX>=20",
        lambda r:
            r["adx"] >= 20
    )
)

tests.append(
    summarize(
        "DI MATCH",
        lambda r:
            r["di_matches"]
    )
)

tests.append(
    summarize(
        "ADX>=20 + DI MATCH",
        lambda r:
            r["adx"] >= 20
            and r["di_matches"]
    )
)

for window in (3, 4, 5):

    tests.append(
        summarize(
            f"ADX>=20 + DI + RISING {window}",
            lambda r, w=window:
                r["adx"] >= 20
                and r["di_matches"]
                and r[f"adx{w}"] == "ALL_RISING"
        )
    )

print()
print("=" * 150)
print("STRATEGY COMPARISON")
print("=" * 150)

print(
    f"{'RULE':<36}"
    f"{'TRADES':>8}"
    f"{'REJECT':>8}"
    f"{'W':>6}"
    f"{'L':>6}"
    f"{'WIN%':>9}"
    f"{'NET PNL':>14}"
)

print("-" * 150)

for x in tests:
    print(
        f"{x['name']:<36}"
        f"{x['trades']:>8}"
        f"{x['rejected']:>8}"
        f"{x['wins']:>6}"
        f"{x['losses']:>6}"
        f"{x['win_rate']:>8.2f}%"
        f"₹{x['net']:>+12.2f}"
    )

# ------------------------------------------------------------
# Winner vs loser ADX characteristics
# ------------------------------------------------------------

print()
print("=" * 150)
print("WINNERS VS LOSERS")
print("=" * 150)

for label, selector in (
    ("WINNERS", lambda r: r["pnl"] > 0),
    ("LOSERS", lambda r: r["pnl"] < 0),
):

    subset = [
        r for r in results
        if selector(r)
    ]

    if not subset:
        continue

    avg_adx = sum(
        r["adx"] for r in subset
    ) / len(subset)

    di_match = sum(
        r["di_matches"]
        for r in subset
    )

    print()
    print(label)
    print(
        "Trades       =", len(subset)
    )
    print(
        "Average ADX  =", round(avg_adx, 2)
    )
    print(
        "DI matched   =",
        f"{di_match}/{len(subset)}"
    )

    for w in (2, 3, 4, 5):

        rising = sum(
            r[f"adx{w}"] == "ALL_RISING"
            for r in subset
        )

        falling = sum(
            r[f"adx{w}"] == "ALL_FALLING"
            for r in subset
        )

        print(
            f"{w}-candle ADX "
            f"| rising={rising} "
            f"| falling={falling}"
        )

print()
print("=" * 150)
print("ANALYSIS COMPLETE — NO BOT CONFIGURATION CHANGED")
print("=" * 150)

