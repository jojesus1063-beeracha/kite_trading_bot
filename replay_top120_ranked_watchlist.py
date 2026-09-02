import math
from pathlib import Path
from collections import defaultdict

import pandas as pd

from auth import get_kite_client

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0

TOP_N = 120

SL_PCT = 0.004
T1_PCT = 0.005
T2_PCT = 0.010

RPT_PCT = 0.75
MAX_POSITION_PCT = 25.0
MAX_DAILY_LOSS_PCT = 0.50

DECISIONS = Path(
    "runtime/watchlist_missed_opportunity/"
    "all_watchlist_decisions.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_ranked_watchlist"
)
OUT.mkdir(parents=True, exist_ok=True)

CANDLE_CACHE = OUT / "candles"
CANDLE_CACHE.mkdir(parents=True, exist_ok=True)

MARGIN_CACHE = OUT / "margin_per_share.csv"


def score_stock(momentum, rvol):
    # Highest-priority sweet spot
    if 1.00 <= momentum < 1.50 and 1.50 <= rvol < 2.00:
        return 100

    # Strong adjacent zone
    if 1.00 <= momentum < 1.50 and 2.00 <= rvol < 3.00:
        return 90

    # Good but less ideal
    if 1.00 <= momentum < 1.50 and 1.00 <= rvol < 1.50:
        return 80

    # Lower momentum but acceptable participation
    if 0.75 <= momentum < 1.00 and 1.50 <= rvol < 3.00:
        return 70

    # Moderate zone
    if 0.75 <= momentum < 1.50 and 0.70 <= rvol < 1.00:
        return 60

    # Still acceptable
    if 0.50 <= momentum < 1.00 and 1.00 <= rvol < 1.50:
        return 50

    # High momentum can become unstable
    if 1.50 <= momentum < 2.00 and 1.00 <= rvol < 3.00:
        return 35

    # Very high momentum or volume
    if momentum >= 2.00 or rvol >= 3.00:
        return 20

    # Everything else
    return 10


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(frame, n=14):
    prev = frame["close"].shift(1)

    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)

    return tr.rolling(n).mean()


def prepare(c):
    c = c.copy()
    c["ema9"] = ema(c["close"], 9)
    c["ema21"] = ema(c["close"], 21)
    c["atr14"] = atr(c, 14)
    return c


def detect_signal(day, i):
    if i < 21:
        return None

    r = day.iloc[i]
    prev = day.iloc[i - 1]

    if not math.isfinite(r["atr14"]) or r["atr14"] <= 0:
        return None

    buy_trend = (
        r["ema9"] > r["ema21"]
        and r["close"] >= r["ema9"]
    )

    sell_trend = (
        r["ema9"] < r["ema21"]
        and r["close"] <= r["ema9"]
    )

    recent = day.iloc[i-20:i]

    buy_breakout = (
        buy_trend
        and r["high"] > recent["high"].max()
        and r["close"] > prev["high"]
    )

    sell_breakout = (
        sell_trend
        and r["low"] < recent["low"].min()
        and r["close"] < prev["low"]
    )

    near_ema_buy = (
        r["low"] <= r["ema9"] * 1.002
        and r["close"] >= r["ema9"]
    )

    near_ema_sell = (
        r["high"] >= r["ema9"] * 0.998
        and r["close"] <= r["ema9"]
    )

    bullish_resumption = (
        r["close"] > r["open"]
        and r["close"] > prev["close"]
    )

    bearish_resumption = (
        r["close"] < r["open"]
        and r["close"] < prev["close"]
    )

    buy_pullback = (
        buy_trend
        and near_ema_buy
        and bullish_resumption
    )

    sell_pullback = (
        sell_trend
        and near_ema_sell
        and bearish_resumption
    )

    if buy_breakout or buy_pullback:
        return {
            "direction": "BUY",
            "entry": float(r["close"]),
            "signal_ts": r["timestamp"],
            "breakout": bool(buy_breakout),
            "pullback": bool(buy_pullback),
        }

    if sell_breakout or sell_pullback:
        return {
            "direction": "SELL",
            "entry": float(r["close"]),
            "signal_ts": r["timestamp"],
            "breakout": bool(sell_breakout),
            "pullback": bool(sell_pullback),
        }

    return None


def estimate_cost(entry, exit_price, qty):
    turnover = (entry + exit_price) * qty

    brokerage = min(
        20.0,
        entry * qty * 0.0003
    ) + min(
        20.0,
        exit_price * qty * 0.0003
    )

    exchange = turnover * 0.0000345
    sebi = turnover * 0.000001
    stamp = entry * qty * 0.00003
    stt = exit_price * qty * 0.00025
    gst = (brokerage + exchange + sebi) * 0.18

    return brokerage + exchange + sebi + stamp + stt + gst


def simulate(day, signal, qty):
    side = signal["direction"]
    entry = signal["entry"]

    if side == "BUY":
        stop = entry * (1 - SL_PCT)
        t1 = entry * (1 + T1_PCT)
        t2 = entry * (1 + T2_PCT)
    else:
        stop = entry * (1 + SL_PCT)
        t1 = entry * (1 - T1_PCT)
        t2 = entry * (1 - T2_PCT)

    q1 = qty // 2
    q2 = qty - q1

    if q1 == 0:
        q1 = 1
        q2 = 0

    gross = 0.0
    costs = 0.0
    t1_hit = False

    after = day[
        day["timestamp"] >= signal["signal_ts"]
    ].copy()

    for _, r in after.iterrows():
        hi = float(r["high"])
        lo = float(r["low"])
        ts = r["timestamp"]

        if not t1_hit:
            if side == "BUY":
                sl_hit = lo <= stop
                t1_now = hi >= t1
            else:
                sl_hit = hi >= stop
                t1_now = lo <= t1

            if sl_hit:
                gross = (
                    (stop-entry)*qty
                    if side == "BUY"
                    else (entry-stop)*qty
                )
                costs = estimate_cost(entry, stop, qty)

                return {
                    "exit": "SL_0.4",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs,
                }

            if t1_now:
                t1_hit = True

                gross += (
                    (t1-entry)*q1
                    if side == "BUY"
                    else (entry-t1)*q1
                )

                costs += estimate_cost(entry, t1, q1)

        else:
            if side == "BUY":
                be_hit = lo <= entry
                t2_hit = hi >= t2
            else:
                be_hit = hi >= entry
                t2_hit = lo <= t2

            if be_hit:
                if q2 > 0:
                    costs += estimate_cost(entry, entry, q2)

                return {
                    "exit": "T1_PLUS_BE",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs,
                }

            if t2_hit:
                if q2 > 0:
                    gross += (
                        (t2-entry)*q2
                        if side == "BUY"
                        else (entry-t2)*q2
                    )
                    costs += estimate_cost(entry, t2, q2)

                return {
                    "exit": "T1_PLUS_T2",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs,
                }

    last = after.iloc[-1]
    eod = float(last["close"])

    if not t1_hit:
        gross = (
            (eod-entry)*qty
            if side == "BUY"
            else (entry-eod)*qty
        )

        costs = estimate_cost(entry, eod, qty)

        return {
            "exit": "EOD_NO_T1",
            "exit_time": last["timestamp"],
            "gross": gross,
            "costs": costs,
            "net": gross-costs,
        }

    if q2 > 0:
        gross += (
            (eod-entry)*q2
            if side == "BUY"
            else (entry-eod)*q2
        )

        costs += estimate_cost(entry, eod, q2)

    return {
        "exit": "T1_PLUS_EOD",
        "exit_time": last["timestamp"],
        "gross": gross,
        "costs": costs,
        "net": gross-costs,
    }


# Load decisions
df = pd.read_csv(DECISIONS)

df["momentum_pct"] = pd.to_numeric(
    df["momentum_pct"], errors="coerce"
)
df["relative_volume"] = pd.to_numeric(
    df["relative_volume"], errors="coerce"
)

df["date"] = pd.to_datetime(
    df["date"], errors="coerce"
).dt.date.astype(str)

df = df.dropna(
    subset=["momentum_pct", "relative_volume"]
)

df["score"] = df.apply(
    lambda r: score_stock(
        r["momentum_pct"],
        r["relative_volume"]
    ),
    axis=1
)

# Tie-breaker:
# prefer momentum nearer 1.25 and RVOL nearer 1.75
df["sweet_distance"] = (
    (df["momentum_pct"] - 1.25).abs()
    +
    (df["relative_volume"] - 1.75).abs() * 0.25
)

ranked_days = []

for date, g in df.groupby("date"):
    top = (
        g.sort_values(
            ["score", "sweet_distance", "momentum_pct"],
            ascending=[False, True, False]
        )
        .drop_duplicates("symbol")
        .head(TOP_N)
        .copy()
    )

    top["rank"] = range(1, len(top)+1)

    ranked_days.append(top)

watchlist = pd.concat(
    ranked_days,
    ignore_index=True
)

watchlist.to_csv(
    OUT / "top120_watchlist.csv",
    index=False
)

print("===== TOP 120 WATCHLIST BY DAY =====")

print(
    watchlist.groupby("date")
    .agg(
        watchlist_size=("symbol", "size"),
        avg_momentum=("momentum_pct", "mean"),
        avg_rvol=("relative_volume", "mean"),
        min_score=("score", "min"),
        max_score=("score", "max"),
    )
    .to_string()
)

# Kite
kite = get_kite_client()

inst = pd.DataFrame(
    kite.instruments("NSE")
)

token_map = dict(
    zip(
        inst["tradingsymbol"].astype(str),
        inst["instrument_token"].astype(int)
    )
)

# Margin cache
margin_map = {}

if MARGIN_CACHE.exists():
    mdf = pd.read_csv(MARGIN_CACHE)

    for _, r in mdf.iterrows():
        try:
            margin_map[str(r["symbol"])] = float(
                r["margin_per_share"]
            )
        except Exception:
            pass

symbols = sorted(
    watchlist["symbol"].astype(str).unique()
)

for symbol in symbols:
    if symbol in margin_map:
        continue

    order = {
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "MIS",
        "order_type": "MARKET",
        "quantity": 1,
    }

    try:
        x = kite.order_margins([order])[0]
        margin_map[symbol] = float(x["total"])
    except Exception:
        pass

pd.DataFrame([
    {
        "symbol": k,
        "margin_per_share": v
    }
    for k, v in sorted(margin_map.items())
]).to_csv(MARGIN_CACHE, index=False)


def load_day(symbol, date):
    p = CANDLE_CACHE / f"{date}_{symbol}.parquet"

    if p.exists():
        c = pd.read_parquet(p)
    else:
        token = token_map.get(symbol)

        if token is None:
            return None

        try:
            raw = kite.historical_data(
                token,
                f"{date} 09:15:00",
                f"{date} 15:30:00",
                "3minute",
                continuous=False,
                oi=False
            )
        except Exception:
            return None

        c = pd.DataFrame(raw)

        if c.empty:
            return None

        c = c.rename(columns={"date": "timestamp"})
        c.to_parquet(p, index=False)

    c["timestamp"] = pd.to_datetime(c["timestamp"])

    return prepare(
        c.sort_values("timestamp")
        .reset_index(drop=True)
    )


# Build signals
signals = []

for n, (_, row) in enumerate(watchlist.iterrows(), 1):
    date = str(row["date"])
    symbol = str(row["symbol"])

    day = load_day(symbol, date)

    if day is None or day.empty:
        continue

    signal = None

    for i in range(len(day)):
        signal = detect_signal(day, i)
        if signal is not None:
            break

    if signal is None:
        continue

    signals.append({
        "date": date,
        "symbol": symbol,
        "rank": row["rank"],
        "score": row["score"],
        "momentum_pct": row["momentum_pct"],
        "relative_volume": row["relative_volume"],
        **signal
    })

signals = pd.DataFrame(signals)

signals.to_csv(
    OUT / "signals.csv",
    index=False
)

# Chronological risk replay
daily_state = defaultdict(
    lambda: {"realized": 0.0}
)

risk_budget = CAPITAL * RPT_PCT / 100
margin_budget = AVAILABLE_MARGIN * MAX_POSITION_PCT / 100
max_daily_loss = CAPITAL * MAX_DAILY_LOSS_PCT / 100

trades = []
blocked = []

signals = signals.sort_values(
    ["date", "signal_ts", "rank"]
)

for _, sig in signals.iterrows():

    date = str(sig["date"])
    symbol = str(sig["symbol"])
    entry = float(sig["entry"])

    mps = margin_map.get(symbol)

    if mps is None or mps <= 0:
        continue

    per_share_risk = entry * SL_PCT

    qty_risk = int(
        risk_budget / per_share_risk
    )

    qty_margin = int(
        margin_budget / mps
    )

    qty = min(qty_risk, qty_margin)

    if qty <= 0:
        blocked.append({
            **sig.to_dict(),
            "reason": "ZERO_QTY"
        })
        continue

    proposed_risk = per_share_risk * qty

    loss_used = max(
        0.0,
        -daily_state[date]["realized"]
    )

    if loss_used + proposed_risk > max_daily_loss:
        blocked.append({
            **sig.to_dict(),
            "reason": "DAILY_RISK_BLOCK",
            "qty": qty,
        })
        continue

    day = load_day(symbol, date)

    sim = simulate(day, sig, qty)

    daily_state[date]["realized"] += sim["net"]

    trades.append({
        **sig.to_dict(),
        "qty": qty,
        "proposed_risk": proposed_risk,
        **sim,
    })

trades = pd.DataFrame(trades)
blocked = pd.DataFrame(blocked)

trades.to_csv(
    OUT / "trade_level.csv",
    index=False
)

blocked.to_csv(
    OUT / "blocked.csv",
    index=False
)

if trades.empty:
    raise SystemExit("No executable trades")

trades["winner"] = trades["net"] > 0

daily = (
    trades.groupby("date")
    .agg(
        trades=("symbol", "size"),
        wins=("winner", "sum"),
        gross=("gross", "sum"),
        costs=("costs", "sum"),
        net=("net", "sum"),
    )
    .reset_index()
)

daily["losses"] = daily["trades"] - daily["wins"]

daily["win_rate"] = (
    daily["wins"]
    / daily["trades"]
    * 100
)

wl_count = (
    watchlist.groupby("date")
    .size()
    .rename("watchlist_size")
)

sig_count = (
    signals.groupby("date")
    .size()
    .rename("signals")
)

daily = (
    daily
    .merge(wl_count, on="date", how="outer")
    .merge(sig_count, on="date", how="outer")
    .fillna(0)
)

print("\n===== TOP120 DAY-WISE PNL =====")

print(
    daily[
        [
            "date",
            "watchlist_size",
            "signals",
            "trades",
            "wins",
            "losses",
            "win_rate",
            "gross",
            "costs",
            "net",
        ]
    ].to_string(
        index=False,
        formatters={
            "win_rate": lambda x: f"{x:.1f}%",
            "gross": lambda x: f"Rs {x:,.2f}",
            "costs": lambda x: f"Rs {x:,.2f}",
            "net": lambda x: f"Rs {x:,.2f}",
        }
    )
)

print("\n===== TOTAL =====")

print("Watchlist rows :", len(watchlist))
print("Signals        :", len(signals))
print("Trades         :", len(trades))

wins = int((trades["net"] > 0).sum())
losses = len(trades) - wins

print("Wins           :", wins)
print("Losses         :", losses)
print("Win rate       :", f"{wins/len(trades)*100:.1f}%")
print("Gross          :", f"Rs {trades['gross'].sum():,.2f}")
print("Charges        :", f"Rs {trades['costs'].sum():,.2f}")
print("NET P&L        :", f"Rs {trades['net'].sum():,.2f}")
print("Blocked        :", len(blocked))

print("\nWrote:", OUT)
