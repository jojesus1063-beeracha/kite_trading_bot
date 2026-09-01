from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

import numpy as np
import pandas as pd

from auth import get_kite_client

IST = ZoneInfo("Asia/Kolkata")

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_stock_direction_test/"
    "feature_level.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison"
)
OUT.mkdir(parents=True, exist_ok=True)

CACHE_ROOT = Path(
    "runtime/trade_replay_history/"
    "index_timeframe_cache"
)
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

EXCLUDED_DATE = "2026-07-30"
INITIAL_TRAIN_END = "2026-08-10"

START_DATE = "2026-07-29"
END_DATE = "2026-08-21"

EMA_FAST = 9
EMA_SLOW = 21
MIN_SPREAD_PCT = 0.05

MIN_EXACT = 3
MIN_NET_ADVANTAGE = 5.0

TIMEFRAMES = {
    "1M": ("minute", 1),
    "3M": ("3minute", 3),
    "5M": ("5minute", 5),
    "15M": ("15minute", 15),
}

INDEXES = {
    "NIFTY50": "NIFTY 50",
    "NIFTYBANK": "NIFTY BANK",
    "NIFTYFINSERVICE": "NIFTY FIN SERVICE",
    "NIFTYIT": "NIFTY IT",
    "NIFTYAUTO": "NIFTY AUTO",
    "NIFTYMETAL": "NIFTY METAL",
    "NIFTYFMCG": "NIFTY FMCG",
    "NIFTYPHARMA": "NIFTY PHARMA",
    "NIFTYHEALTHCARE": "NIFTY HEALTHCARE INDEX",
    "NIFTYENERGY": "NIFTY ENERGY",
    "NIFTYREALTY": "NIFTY REALTY",
    "NIFTYPSUBANK": "NIFTY PSU BANK",
    "NIFTYMEDIA": "NIFTY MEDIA",
}

SECTOR_INDEX = {
    "BANK": "NIFTYBANK",
    "PSUBANK": "NIFTYPSUBANK",
    "FINANCIAL": "NIFTYFINSERVICE",
    "IT": "NIFTYIT",
    "AUTO": "NIFTYAUTO",
    "METAL": "NIFTYMETAL",
    "FMCG": "NIFTYFMCG",
    "PHARMA": "NIFTYPHARMA",
    "HEALTHCARE": "NIFTYHEALTHCARE",
    "ENERGY": "NIFTYENERGY",
    "REALTY": "NIFTYREALTY",
    "MEDIA": "NIFTYMEDIA",
}


def norm(s):
    return (
        str(s).upper()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
    )


def resolve_indices(kite):
    inst = pd.DataFrame(kite.instruments("NSE"))

    inst["name_norm"] = inst["name"].astype(str).map(norm)
    inst["symbol_norm"] = (
        inst["tradingsymbol"].astype(str).map(norm)
    )

    resolved = {}

    for save_name, target in INDEXES.items():
        n = norm(target)

        m = inst[
            (inst["name_norm"] == n)
            |
            (inst["symbol_norm"] == n)
        ]

        if m.empty:
            m = inst[
                inst["name_norm"].str.contains(
                    n,
                    regex=False,
                    na=False,
                )
            ]

        if not m.empty:
            resolved[save_name] = int(
                m.iloc[0]["instrument_token"]
            )

    return resolved


def load_or_download(
    kite,
    token,
    save_name,
    interval,
):
    tf_dir = CACHE_ROOT / interval
    tf_dir.mkdir(parents=True, exist_ok=True)

    p = tf_dir / f"{save_name}.parquet"

    if p.exists():
        x = pd.read_parquet(p)
        if not x.empty:
            x["timestamp"] = pd.to_datetime(
                x["timestamp"]
            )
            return x

    start = datetime.strptime(
        START_DATE,
        "%Y-%m-%d",
    ).replace(
        hour=9,
        minute=15,
        tzinfo=IST,
    )

    end = datetime.strptime(
        END_DATE,
        "%Y-%m-%d",
    ).replace(
        hour=15,
        minute=30,
        tzinfo=IST,
    )

    rows = []
    cur = start

    chunk_days = 8 if interval == "minute" else 15

    while cur <= end:
        chunk_end = min(
            cur + timedelta(days=chunk_days),
            end,
        )

        try:
            data = kite.historical_data(
                token,
                cur,
                chunk_end,
                interval,
                continuous=False,
                oi=False,
            )
        except Exception as e:
            print(
                f"{save_name} {interval}: ERROR {e}"
            )
            data = []

        rows.extend(data)

        cur = chunk_end + timedelta(seconds=1)
        time.sleep(0.35)

    x = pd.DataFrame(rows)

    if x.empty:
        return x

    if "date" in x.columns:
        x = x.rename(
            columns={"date": "timestamp"}
        )

    x["timestamp"] = pd.to_datetime(
        x["timestamp"]
    )

    x = (
        x.drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    x.to_parquet(
        p,
        index=False,
    )

    return x


def completed_trend(
    candles,
    signal_ts,
    timeframe_minutes,
):
    if candles is None or candles.empty:
        return "UNKNOWN"

    x = candles.copy()

    x["timestamp"] = pd.to_datetime(
        x["timestamp"]
    )

    ts = pd.Timestamp(signal_ts)

    candle_tz = x["timestamp"].dt.tz

    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize(
                "Asia/Kolkata"
            )
        else:
            ts = ts.tz_convert(
                candle_tz
            )

    # candle timestamp = candle OPEN time
    # so only use a candle after it has fully completed
    cutoff = ts - pd.Timedelta(
        minutes=timeframe_minutes
    )

    hist = x[
        x["timestamp"] <= cutoff
    ].copy()

    if len(hist) < EMA_SLOW:
        return "UNKNOWN"

    close = pd.to_numeric(
        hist["close"],
        errors="coerce",
    )

    fast = close.ewm(
        span=EMA_FAST,
        adjust=False,
    ).mean().iloc[-1]

    slow = close.ewm(
        span=EMA_SLOW,
        adjust=False,
    ).mean().iloc[-1]

    last = float(close.iloc[-1])

    if (
        last <= 0
        or pd.isna(fast)
        or pd.isna(slow)
    ):
        return "UNKNOWN"

    spread = (
        (float(fast) - float(slow))
        / last
        * 100.0
    )

    if spread >= MIN_SPREAD_PCT:
        return "BULLISH"

    if spread <= -MIN_SPREAD_PCT:
        return "BEARISH"

    return "SIDEWAYS"


def choose_exact(
    train,
    row,
    market_col,
    sector_col,
):
    g = train[
        (train[market_col] == row[market_col])
        &
        (train[sector_col] == row[sector_col])
        &
        (train["stock_trend"] == row["stock_trend"])
        &
        (train["direction"] == row["direction"])
    ]

    if len(g) < MIN_EXACT:
        return "SKIP"

    normal_net = float(
        g["normal_net"].sum()
    )

    reverse_net = float(
        g["reverse_net"].sum()
    )

    if (
        abs(normal_net - reverse_net)
        <
        MIN_NET_ADVANTAGE
    ):
        return "SKIP"

    if (
        normal_net > 0
        and normal_net > reverse_net
    ):
        return "NORMAL"

    if (
        reverse_net > 0
        and reverse_net > normal_net
    ):
        return "REVERSE"

    return "SKIP"


def run_walkforward(
    df,
    market_col,
    sector_col,
):
    rows = []

    test_dates = sorted(
        d
        for d in df["date"].unique()
        if d > INITIAL_TRAIN_END
    )

    for d in test_dates:
        train = df[
            df["date"] < d
        ]

        today = df[
            df["date"] == d
        ]

        for _, r in today.iterrows():
            decision = choose_exact(
                train,
                r,
                market_col,
                sector_col,
            )

            if decision == "SKIP":
                continue

            if decision == "NORMAL":
                gross = float(r["normal_gross"])
                costs = float(r["normal_costs"])
                net = float(r["normal_net"])
            else:
                gross = float(r["reverse_gross"])
                costs = float(r["reverse_costs"])
                net = float(r["reverse_net"])

            rows.append({
                "date": d,
                "signal_ts": r["signal_ts"],
                "symbol": r["symbol"],
                "decision": decision,
                "gross": gross,
                "costs": costs,
                "net": net,
            })

    return pd.DataFrame(rows)


def metrics(x):
    if x.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "gross": 0.0,
            "costs": 0.0,
            "net": 0.0,
            "avg_net": 0.0,
            "max_drawdown": 0.0,
            "profitable_days": 0,
            "losing_days": 0,
        }

    x = x.sort_values(
        ["date", "signal_ts"]
    ).copy()

    wins = int(
        (x["net"] > 0).sum()
    )

    equity = x["net"].cumsum()
    peak = equity.cummax()

    dd = float(
        (equity - peak).min()
    )

    daily = (
        x.groupby("date")["net"]
        .sum()
    )

    return {
        "trades":
            len(x),

        "wins":
            wins,

        "losses":
            len(x)-wins,

        "win_rate_pct":
            wins/len(x)*100.0,

        "gross":
            float(x["gross"].sum()),

        "costs":
            float(x["costs"].sum()),

        "net":
            float(x["net"].sum()),

        "avg_net":
            float(x["net"].mean()),

        "max_drawdown":
            dd,

        "profitable_days":
            int((daily > 0).sum()),

        "losing_days":
            int((daily < 0).sum()),
    }


# --------------------------------------------------
# SOURCE
# --------------------------------------------------

df = pd.read_csv(BASE)

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce",
).dt.strftime("%Y-%m-%d")

df["signal_ts"] = pd.to_datetime(
    df["signal_ts"],
    errors="coerce",
)

df = df[
    df["date"] != EXCLUDED_DATE
].copy()

print("===== SOURCE =====")
print("Rows :", len(df))
print("Dates:", df["date"].nunique())


# --------------------------------------------------
# INDEX DATA
# --------------------------------------------------

kite = get_kite_client()

resolved = resolve_indices(kite)

print("\n===== INDEX RESOLUTION =====")

for name in INDEXES:
    print(
        f"{name:20s}",
        "OK"
        if name in resolved
        else "NOT FOUND"
    )

cache = {}

for tf_name, (interval, minutes) in TIMEFRAMES.items():

    cache[tf_name] = {}

    print(
        f"\n===== LOADING {tf_name} INDEX DATA ====="
    )

    for index_name, token in resolved.items():

        x = load_or_download(
            kite,
            token,
            index_name,
            interval,
        )

        cache[tf_name][index_name] = x

        print(
            f"{index_name:20s} rows={len(x)}"
        )


# --------------------------------------------------
# BUILD TREND FEATURES
# --------------------------------------------------

for tf_name, (_, minutes) in TIMEFRAMES.items():

    market_values = []
    sector_values = []

    for _, r in df.iterrows():

        market_values.append(
            completed_trend(
                cache[tf_name].get(
                    "NIFTY50"
                ),
                r["signal_ts"],
                minutes,
            )
        )

        sector_index = (
            SECTOR_INDEX.get(
                r["sector"]
            )
        )

        if sector_index is None:
            sector_values.append(
                "UNKNOWN"
            )
        else:
            sector_values.append(
                completed_trend(
                    cache[tf_name].get(
                        sector_index
                    ),
                    r["signal_ts"],
                    minutes,
                )
            )

    df[
        f"{tf_name}_market"
    ] = market_values

    df[
        f"{tf_name}_sector"
    ] = sector_values


# --------------------------------------------------
# RUN ALL 4
# --------------------------------------------------

results = {}
summary_rows = []

for tf_name in TIMEFRAMES:

    trades = run_walkforward(
        df,
        f"{tf_name}_market",
        f"{tf_name}_sector",
    )

    results[tf_name] = trades

    m = metrics(trades)

    summary_rows.append({
        "setup":
            f"{tf_name}/{tf_name}/3M",
        **m,
    })

    trades.to_csv(
        OUT / f"trades_{tf_name.lower()}.csv",
        index=False,
    )


summary = pd.DataFrame(
    summary_rows
)

summary["rank"] = (
    summary["net"]
    .rank(
        ascending=False,
        method="min",
    )
    .astype(int)
)

summary = summary.sort_values(
    ["rank", "max_drawdown"],
    ascending=[True, False],
)

print(
    "\n===== TIMEFRAME RANKING ====="
)

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v:
                    f"{v:.1f}%",

            "gross":
                lambda v:
                    f"Rs {v:.2f}",

            "costs":
                lambda v:
                    f"Rs {v:.2f}",

            "net":
                lambda v:
                    f"Rs {v:.2f}",

            "avg_net":
                lambda v:
                    f"Rs {v:.2f}",

            "max_drawdown":
                lambda v:
                    f"Rs {v:.2f}",
        }
    )
)


# --------------------------------------------------
# DAYWISE
# --------------------------------------------------

for tf_name, trades in results.items():

    print(
        f"\n===== {tf_name}/{tf_name}/3M DAYWISE ====="
    )

    if trades.empty:
        print("None")
        continue

    day = (
        trades.groupby("date")
        .agg(
            trades=("symbol", "size"),
            wins=(
                "net",
                lambda s:
                    int((s > 0).sum())
            ),
            losses=(
                "net",
                lambda s:
                    int((s <= 0).sum())
            ),
            net=("net", "sum"),
        )
        .reset_index()
    )

    day["win_rate_pct"] = (
        day["wins"]
        /
        day["trades"]
        * 100.0
    )

    day["cumulative_net"] = (
        day["net"].cumsum()
    )

    print(
        day.to_string(
            index=False,
            formatters={
                "win_rate_pct":
                    lambda v:
                        f"{v:.1f}%",

                "net":
                    lambda v:
                        f"Rs {v:.2f}",

                "cumulative_net":
                    lambda v:
                        f"Rs {v:.2f}",
            }
        )
    )

    day.to_csv(
        OUT / f"daywise_{tf_name.lower()}.csv",
        index=False,
    )


# --------------------------------------------------
# TREND DIFFERENCE VS 3M
# --------------------------------------------------

print(
    "\n===== TREND DIFFERENCE VS 3M ====="
)

for tf_name in [
    "1M",
    "5M",
    "15M",
]:

    market_diff = int(
        (
            df[f"{tf_name}_market"]
            !=
            df["3M_market"]
        ).sum()
    )

    sector_diff = int(
        (
            df[f"{tf_name}_sector"]
            !=
            df["3M_sector"]
        ).sum()
    )

    print(
        f"{tf_name}: "
        f"market changed {market_diff}/{len(df)} | "
        f"sector changed {sector_diff}/{len(df)}"
    )


df.to_csv(
    OUT / "feature_comparison.csv",
    index=False,
)

summary.to_csv(
    OUT / "summary.csv",
    index=False,
)

print(
    "\nWrote:",
    OUT
)
