import math
from pathlib import Path
import pandas as pd

REJECTED = Path(
    "runtime/watchlist_missed_opportunity/rejected_stocks.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/candles_3minute"
)

OUTDIR = Path(
    "runtime/watchlist_missed_opportunity/replay"
)
OUTDIR.mkdir(parents=True, exist_ok=True)

SL_PCT = 0.005
T1_PCT = 0.005
T2_PCT = 0.010

df = pd.read_csv(REJECTED)

for c in [
    "momentum_pct",
    "relative_volume",
    "last_price",
]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce"
).dt.date.astype(str)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(frame, n=14):
    prev_close = frame["close"].shift(1)

    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.rolling(n).mean()


def prepare(c):
    c = c.copy()

    c["ema9"] = ema(c["close"], 9)
    c["ema21"] = ema(c["close"], 21)
    c["atr14"] = atr(c, 14)

    typical = (
        c["high"] + c["low"] + c["close"]
    ) / 3.0

    pv = typical * c["volume"]

    c["vwap"] = (
        pv.groupby(c["timestamp"].dt.date).cumsum()
        /
        c["volume"].groupby(
            c["timestamp"].dt.date
        ).cumsum()
    )

    body = (c["close"] - c["open"]).abs()

    c["body"] = body

    c["lower_wick"] = (
        c[["open", "close"]].min(axis=1)
        - c["low"]
    )

    c["upper_wick"] = (
        c["high"]
        - c[["open", "close"]].max(axis=1)
    )

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

    # -------------------------
    # BREAKOUT
    # -------------------------
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

    # -------------------------
    # PULLBACK
    # -------------------------
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
            "breakout": buy_breakout,
            "pullback": buy_pullback,
            "entry": float(r["close"]),
            "signal_ts": r["timestamp"],
        }

    if sell_breakout or sell_pullback:
        return {
            "direction": "SELL",
            "breakout": sell_breakout,
            "pullback": sell_pullback,
            "entry": float(r["close"]),
            "signal_ts": r["timestamp"],
        }

    return None


def simulate(day, signal):
    side = signal["direction"]
    entry = signal["entry"]

    # Keep quantity neutral here:
    # one-share P&L allows clean comparison
    qty = 1

    if side == "BUY":
        stop = entry * (1 - SL_PCT)
        t1 = entry * (1 + T1_PCT)
        t2 = entry * (1 + T2_PCT)
    else:
        stop = entry * (1 + SL_PCT)
        t1 = entry * (1 - T1_PCT)
        t2 = entry * (1 - T2_PCT)

    q1 = 1
    q2 = 1

    t1_hit = False
    gross = 0.0

    after = day[
        day["timestamp"] >= signal["signal_ts"]
    ].copy()

    for _, r in after.iterrows():
        hi = float(r["high"])
        lo = float(r["low"])
        ts = r["timestamp"]

        if not t1_hit:
            if side == "BUY":
                stop_hit = lo <= stop
                t1_hit_now = hi >= t1
            else:
                stop_hit = hi >= stop
                t1_hit_now = lo <= t1

            # conservative same-candle ambiguity
            if stop_hit:
                gross = (
                    stop - entry
                    if side == "BUY"
                    else entry - stop
                )

                return {
                    "exit": "SL_0.5",
                    "exit_time": ts,
                    "gross_per_share": gross,
                }

            if t1_hit_now:
                gross += (
                    t1 - entry
                    if side == "BUY"
                    else entry - t1
                )

                t1_hit = True

        else:
            if side == "BUY":
                be_hit = lo <= entry
                t2_hit = hi >= t2
            else:
                be_hit = hi >= entry
                t2_hit = lo <= t2

            if be_hit:
                return {
                    "exit": "T1_PLUS_BE",
                    "exit_time": ts,
                    "gross_per_share": gross / 2.0,
                }

            if t2_hit:
                gross += (
                    t2 - entry
                    if side == "BUY"
                    else entry - t2
                )

                return {
                    "exit": "T1_PLUS_T2",
                    "exit_time": ts,
                    "gross_per_share": gross / 2.0,
                }

    last = after.iloc[-1]

    eod = float(last["close"])

    if not t1_hit:
        gross = (
            eod - entry
            if side == "BUY"
            else entry - eod
        )

        return {
            "exit": "EOD_NO_T1",
            "exit_time": last["timestamp"],
            "gross_per_share": gross,
        }

    runner = (
        eod - entry
        if side == "BUY"
        else entry - eod
    )

    return {
        "exit": "T1_PLUS_EOD",
        "exit_time": last["timestamp"],
        "gross_per_share": (
            (
                (
                    t1 - entry
                    if side == "BUY"
                    else entry - t1
                )
                + runner
            )
            / 2.0
        ),
    }


results = []

unique = (
    df[[
        "date",
        "symbol",
        "momentum_pct",
        "relative_volume",
        "watchlist_group",
    ]]
    .drop_duplicates()
)

print("Rejected symbol/date pairs:", len(unique))

for n, (_, row) in enumerate(
    unique.iterrows(),
    start=1
):
    symbol = str(row["symbol"])
    date = str(row["date"])

    p = CANDLE_DIR / f"{symbol}.parquet"

    if not p.exists():
        results.append({
            **row.to_dict(),
            "status": "NO_CANDLES",
        })
        continue

    c = pd.read_parquet(p)

    time_col = (
        "timestamp"
        if "timestamp" in c.columns
        else "date"
    )

    c["timestamp"] = pd.to_datetime(
        c[time_col],
        errors="coerce"
    )

    day = c[
        c["timestamp"].dt.date.astype(str) == date
    ].copy()

    if day.empty:
        results.append({
            **row.to_dict(),
            "status": "NO_DAY_CANDLES",
        })
        continue

    day = (
        day.sort_values("timestamp")
        .reset_index(drop=True)
    )

    day = prepare(day)

    signal = None

    for i in range(len(day)):
        candidate = detect_signal(day, i)

        if candidate is not None:
            signal = candidate
            break

    if signal is None:
        results.append({
            **row.to_dict(),
            "status": "NO_BP_SIGNAL",
        })
        continue

    sim = simulate(day, signal)

    results.append({
        **row.to_dict(),

        "status": "TRADE",

        "signal_time":
            signal["signal_ts"].strftime("%H:%M"),

        "direction":
            signal["direction"],

        "entry":
            signal["entry"],

        "breakout":
            signal["breakout"],

        "pullback":
            signal["pullback"],

        "exit":
            sim["exit"],

        "exit_time":
            sim["exit_time"],

        "gross_per_share":
            sim["gross_per_share"],
    })

    if n % 50 == 0:
        print(
            f"Processed {n}/{len(unique)}"
        )


r = pd.DataFrame(results)

r.to_csv(
    OUTDIR / "all_rejected_replay.csv",
    index=False
)

trades = r[
    r["status"] == "TRADE"
].copy()

trades["winner"] = (
    trades["gross_per_share"] > 0
)

# --------------------------------------------------
# RVOL BANDS
# --------------------------------------------------

bins = [
    -float("inf"),
    0.40,
    0.70,
    1.00,
    1.50,
    2.00,
    3.00,
    5.00,
    10.00,
    float("inf"),
]

labels = [
    "<0.40",
    "0.40-0.70",
    "0.70-1.00",
    "1.00-1.50",
    "1.50-2.00",
    "2.00-3.00",
    "3.00-5.00",
    "5.00-10.00",
    ">10.00",
]

trades["rvol_band"] = pd.cut(
    trades["relative_volume"],
    bins=bins,
    labels=labels,
    right=False,
)

summary = (
    trades.groupby(
        "rvol_band",
        observed=False
    )
    .agg(
        trades=("winner", "size"),
        winners=("winner", "sum"),
        gross_per_share=(
            "gross_per_share",
            "sum"
        ),
        avg_gross_per_share=(
            "gross_per_share",
            "mean"
        ),
    )
    .reset_index()
)

summary["losers"] = (
    summary["trades"]
    - summary["winners"]
)

summary["win_rate_pct"] = (
    summary["winners"]
    / summary["trades"]
    * 100
)

summary.to_csv(
    OUTDIR / "rvol_band_summary.csv",
    index=False
)

# --------------------------------------------------
# REJECTION REASON SUMMARY
# --------------------------------------------------

reason = (
    trades.groupby("watchlist_group")
    .agg(
        trades=("winner", "size"),
        winners=("winner", "sum"),
        gross_per_share=(
            "gross_per_share",
            "sum"
        ),
    )
    .reset_index()
)

reason["losers"] = (
    reason["trades"]
    - reason["winners"]
)

reason["win_rate_pct"] = (
    reason["winners"]
    / reason["trades"]
    * 100
)

reason.to_csv(
    OUTDIR / "rejection_reason_summary.csv",
    index=False
)

# --------------------------------------------------
# PROFITABLE MISSED OPPORTUNITIES
# --------------------------------------------------

missed_winners = trades[
    trades["gross_per_share"] > 0
].copy()

missed_winners = missed_winners.sort_values(
    "gross_per_share",
    ascending=False
)

missed_winners.to_csv(
    OUTDIR / "missed_profitable_trades.csv",
    index=False
)

print("\n===== REPLAY COVERAGE =====")
print(
    r["status"]
    .value_counts(dropna=False)
    .to_string()
)

print("\n===== REJECTED WATCHLIST / RVOL RESULTS =====")

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x: f"{x:.1f}%",

            "gross_per_share":
                lambda x: f"{x:.4f}",

            "avg_gross_per_share":
                lambda x: f"{x:.4f}",
        }
    )
)

print("\n===== RESULTS BY REJECTION REASON =====")

print(
    reason.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x: f"{x:.1f}%",

            "gross_per_share":
                lambda x: f"{x:.4f}",
        }
    )
)

print("\n===== TOP MISSED PROFITABLE TRADES =====")

print(
    missed_winners[
        [
            "date",
            "symbol",
            "signal_time",
            "direction",
            "momentum_pct",
            "relative_volume",
            "rvol_band",
            "entry",
            "exit",
            "gross_per_share",
        ]
    ]
    .head(50)
    .to_string(index=False)
)

print("\nWROTE:", OUTDIR)
