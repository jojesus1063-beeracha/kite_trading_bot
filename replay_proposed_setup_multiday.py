import math
from pathlib import Path
from collections import defaultdict

import pandas as pd

from auth import get_kite_client

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0

MOM_MIN = 1.00
MOM_MAX = 1.50

RVOL_MIN = 1.50
RVOL_MAX = 2.00

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
    "proposed_setup_multiday"
)
OUT.mkdir(parents=True, exist_ok=True)

CANDLE_CACHE = OUT / "candles"
CANDLE_CACHE.mkdir(parents=True, exist_ok=True)

MARGIN_CACHE = OUT / "margin_per_share.csv"


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
        high = float(r["high"])
        low = float(r["low"])
        ts = r["timestamp"]

        if not t1_hit:

            if side == "BUY":
                sl_hit = low <= stop
                t1_now = high >= t1
            else:
                sl_hit = high >= stop
                t1_now = low <= t1

            # conservative same-candle ambiguity
            if sl_hit:
                gross = (
                    (stop-entry) * qty
                    if side == "BUY"
                    else (entry-stop) * qty
                )

                costs = estimate_cost(
                    entry,
                    stop,
                    qty
                )

                return {
                    "exit": "SL_0.4",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross - costs,
                }

            if t1_now:
                t1_hit = True

                gross += (
                    (t1-entry) * q1
                    if side == "BUY"
                    else (entry-t1) * q1
                )

                costs += estimate_cost(
                    entry,
                    t1,
                    q1
                )

                if q2 == 0:
                    return {
                        "exit": "T1_ONLY",
                        "exit_time": ts,
                        "gross": gross,
                        "costs": costs,
                        "net": gross-costs,
                    }

        else:
            if side == "BUY":
                be_hit = low <= entry
                t2_hit = high >= t2
            else:
                be_hit = high >= entry
                t2_hit = low <= t2

            if be_hit:
                costs += estimate_cost(
                    entry,
                    entry,
                    q2
                )

                return {
                    "exit": "T1_PLUS_BE",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs,
                }

            if t2_hit:
                gross += (
                    (t2-entry) * q2
                    if side == "BUY"
                    else (entry-t2) * q2
                )

                costs += estimate_cost(
                    entry,
                    t2,
                    q2
                )

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
            (eod-entry) * qty
            if side == "BUY"
            else (entry-eod) * qty
        )

        costs = estimate_cost(
            entry,
            eod,
            qty
        )

        return {
            "exit": "EOD_NO_T1",
            "exit_time": last["timestamp"],
            "gross": gross,
            "costs": costs,
            "net": gross-costs,
        }

    gross += (
        (eod-entry) * q2
        if side == "BUY"
        else (entry-eod) * q2
    )

    costs += estimate_cost(
        entry,
        eod,
        q2
    )

    return {
        "exit": "T1_PLUS_EOD",
        "exit_time": last["timestamp"],
        "gross": gross,
        "costs": costs,
        "net": gross-costs,
    }


# --------------------------------------------------
# Load watchlist decisions
# --------------------------------------------------

df = pd.read_csv(DECISIONS)

for c in [
    "momentum_pct",
    "relative_volume",
]:
    df[c] = pd.to_numeric(
        df[c],
        errors="coerce"
    )

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce"
).dt.date.astype(str)

zone = df[
    (df["momentum_pct"] >= MOM_MIN)
    &
    (df["momentum_pct"] < MOM_MAX)
    &
    (df["relative_volume"] >= RVOL_MIN)
    &
    (df["relative_volume"] < RVOL_MAX)
].copy()

zone = zone.drop_duplicates(
    ["date", "symbol"]
)

print("===== TARGET ZONE =====")
print("Rows :", len(zone))
print("Dates:", zone["date"].nunique())

print(
    zone.groupby("date")
    .size()
    .rename("watchlist_candidates")
    .to_string()
)

# --------------------------------------------------
# Kite + instruments
# --------------------------------------------------

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

# --------------------------------------------------
# Margin map
# --------------------------------------------------

margin_map = {}

if MARGIN_CACHE.exists():
    m = pd.read_csv(MARGIN_CACHE)

    for _, r in m.iterrows():
        try:
            margin_map[str(r["symbol"])] = float(
                r["margin_per_share"]
            )
        except Exception:
            pass

symbols = sorted(
    zone["symbol"].astype(str).unique()
)

missing_margin = [
    s for s in symbols
    if s not in margin_map
]

for i, symbol in enumerate(
    missing_margin,
    1
):

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

        margin_map[symbol] = float(
            x["total"]
        )

        print(
            f"MARGIN {i}/{len(missing_margin)} "
            f"{symbol}: "
            f"{margin_map[symbol]:.2f}"
        )

    except Exception as e:
        print(
            "MARGIN ERROR",
            symbol,
            e
        )

pd.DataFrame([
    {
        "symbol": s,
        "margin_per_share": v
    }
    for s, v in sorted(margin_map.items())
]).to_csv(
    MARGIN_CACHE,
    index=False
)

# --------------------------------------------------
# Candle download/cache
# --------------------------------------------------

def load_day(symbol, date):
    p = (
        CANDLE_CACHE /
        f"{date}_{symbol}.parquet"
    )

    if p.exists():
        c = pd.read_parquet(p)
    else:
        token = token_map.get(symbol)

        if token is None:
            return None

        start = f"{date} 09:15:00"
        end = f"{date} 15:30:00"

        try:
            raw = kite.historical_data(
                token,
                start,
                end,
                "3minute",
                continuous=False,
                oi=False,
            )
        except Exception as e:
            print(
                "CANDLE ERROR",
                date,
                symbol,
                e
            )
            return None

        c = pd.DataFrame(raw)

        if c.empty:
            return None

        c = c.rename(
            columns={"date": "timestamp"}
        )

        c.to_parquet(
            p,
            index=False
        )

    c["timestamp"] = pd.to_datetime(
        c["timestamp"]
    )

    c = (
        c.sort_values("timestamp")
        .reset_index(drop=True)
    )

    return prepare(c)


# --------------------------------------------------
# Build candidate signals first
# --------------------------------------------------

signals = []

for n, (_, row) in enumerate(
    zone.iterrows(),
    1
):
    date = str(row["date"])
    symbol = str(row["symbol"])

    day = load_day(
        symbol,
        date
    )

    if day is None or day.empty:
        continue

    signal = None

    for i in range(len(day)):
        x = detect_signal(
            day,
            i
        )

        if x is not None:
            signal = x
            break

    if signal is None:
        continue

    signals.append({
        "date": date,
        "symbol": symbol,
        "momentum_pct":
            float(row["momentum_pct"]),
        "relative_volume":
            float(row["relative_volume"]),
        **signal,
    })

    if n % 20 == 0:
        print(
            f"Processed {n}/{len(zone)}"
        )

signals = pd.DataFrame(signals)

if signals.empty:
    raise SystemExit(
        "No Breakout/Pullback signals generated."
    )

signals = signals.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

signals.to_csv(
    OUT / "signals.csv",
    index=False
)

# --------------------------------------------------
# Chronological risk/position replay
# --------------------------------------------------

daily_state = defaultdict(
    lambda: {
        "realized": 0.0,
        "trades": 0,
    }
)

trade_rows = []
blocked_rows = []

max_daily_loss = (
    CAPITAL
    * MAX_DAILY_LOSS_PCT
    / 100.0
)

risk_budget = (
    CAPITAL
    * RPT_PCT
    / 100.0
)

margin_budget = (
    AVAILABLE_MARGIN
    * MAX_POSITION_PCT
    / 100.0
)

for _, sig in signals.iterrows():

    date = str(sig["date"])
    symbol = str(sig["symbol"])
    entry = float(sig["entry"])

    state = daily_state[date]

    mps = margin_map.get(symbol)

    if (
        mps is None
        or not math.isfinite(mps)
        or mps <= 0
    ):
        continue

    per_share_risk = (
        entry * SL_PCT
    )

    qty_risk = int(
        risk_budget
        / per_share_risk
    )

    qty_margin = int(
        margin_budget
        / mps
    )

    qty = min(
        qty_risk,
        qty_margin
    )

    if qty <= 0:
        blocked_rows.append({
            **sig.to_dict(),
            "reason": "ZERO_QTY"
        })
        continue

    proposed_risk = (
        per_share_risk * qty
    )

    realized_loss_used = max(
        0.0,
        -state["realized"]
    )

    if (
        realized_loss_used
        + proposed_risk
        > max_daily_loss
        + 1e-9
    ):
        blocked_rows.append({
            **sig.to_dict(),
            "reason":
                "DAILY_RISK_BLOCK",
            "qty": qty,
            "proposed_risk":
                proposed_risk,
        })
        continue

    day = load_day(
        symbol,
        date
    )

    sim = simulate(
        day,
        sig,
        qty
    )

    state["realized"] += (
        sim["net"]
    )

    state["trades"] += 1

    trade_rows.append({
        **sig.to_dict(),
        "qty": qty,
        "margin_per_share": mps,
        "proposed_risk":
            proposed_risk,
        **sim,
    })

trades = pd.DataFrame(trade_rows)
blocked = pd.DataFrame(blocked_rows)

trades.to_csv(
    OUT / "trade_level.csv",
    index=False
)

blocked.to_csv(
    OUT / "blocked.csv",
    index=False
)

# --------------------------------------------------
# Daily report
# --------------------------------------------------

if trades.empty:
    raise SystemExit(
        "No executable trades."
    )

trades["winner"] = (
    trades["net"] > 0
)

daily = (
    trades.groupby("date")
    .agg(
        watchlist_trades=(
            "symbol",
            "size"
        ),
        wins=(
            "winner",
            "sum"
        ),
        gross=(
            "gross",
            "sum"
        ),
        costs=(
            "costs",
            "sum"
        ),
        net=(
            "net",
            "sum"
        ),
    )
    .reset_index()
)

daily["losses"] = (
    daily["watchlist_trades"]
    - daily["wins"]
)

daily["win_rate_pct"] = (
    daily["wins"]
    / daily["watchlist_trades"]
    * 100
)

candidate_count = (
    zone.groupby("date")
    .size()
    .rename("watchlist_candidates")
)

signal_count = (
    signals.groupby("date")
    .size()
    .rename("bp_signals")
)

daily = (
    daily
    .merge(
        candidate_count,
        on="date",
        how="outer"
    )
    .merge(
        signal_count,
        on="date",
        how="outer"
    )
    .fillna(0)
)

daily = daily[
    [
        "date",
        "watchlist_candidates",
        "bp_signals",
        "watchlist_trades",
        "wins",
        "losses",
        "win_rate_pct",
        "gross",
        "costs",
        "net",
    ]
]

daily.to_csv(
    OUT / "daily.csv",
    index=False
)

print(
    "\n===== DAY-WISE PROPOSED SETUP PNL ====="
)

print(
    daily.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x: f"{x:.1f}%",

            "gross":
                lambda x: f"Rs {x:,.2f}",

            "costs":
                lambda x: f"Rs {x:,.2f}",

            "net":
                lambda x: f"Rs {x:,.2f}",
        }
    )
)

print(
    "\n===== TOTAL ====="
)

print("Days           :", len(daily))
print("Trades         :", len(trades))

wins = int(
    (trades["net"] > 0).sum()
)

losses = len(trades) - wins

print("Wins           :", wins)
print("Losses         :", losses)

print(
    "Win rate       :",
    f"{wins/len(trades)*100:.1f}%"
)

print(
    "Gross P&L      :",
    f"Rs {trades['gross'].sum():,.2f}"
)

print(
    "Charges        :",
    f"Rs {trades['costs'].sum():,.2f}"
)

print(
    "NET P&L        :",
    f"Rs {trades['net'].sum():,.2f}"
)

print(
    "Avg net/trade  :",
    f"Rs {trades['net'].mean():,.2f}"
)

print(
    "Profitable days:",
    int((daily["net"] > 0).sum())
)

print(
    "Losing days    :",
    int((daily["net"] < 0).sum())
)

print(
    "Blocked trades :",
    len(blocked)
)

print("\nWrote:", OUT)
