from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import math

import numpy as np
import pandas as pd

from auth import get_kite_client

IST = ZoneInfo("Asia/Kolkata")

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_stock_direction_test/"
    "feature_level.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)

ONE_MIN_DIR = Path(
    "runtime/trade_replay_history/"
    "candles_1minute_indices"
)
ONE_MIN_DIR.mkdir(parents=True, exist_ok=True)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "compare_1m_vs_3m_market_sector"
)
OUT.mkdir(parents=True, exist_ok=True)

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
    "1M": "minute",
    "3M": "3minute",
    "5M": "5minute",
    "15M": "15minute",
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

    inst["name_norm"] = (
        inst["name"].astype(str).map(norm)
    )

    inst["symbol_norm"] = (
        inst["tradingsymbol"]
        .astype(str)
        .map(norm)
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
                    na=False
                )
            ]

        if not m.empty:
            resolved[save_name] = int(
                m.iloc[0]["instrument_token"]
            )

    return resolved


def download_1m(kite, token, save_name):
    p = ONE_MIN_DIR / f"{save_name}.parquet"

    if p.exists():
        x = pd.read_parquet(p)
        if not x.empty:
            return x

    start = datetime.strptime(
        START_DATE,
        "%Y-%m-%d"
    ).replace(
        hour=9,
        minute=15,
        tzinfo=IST
    )

    end = datetime.strptime(
        END_DATE,
        "%Y-%m-%d"
    ).replace(
        hour=15,
        minute=30,
        tzinfo=IST
    )

    rows = []
    cur = start

    # Small chunks because 1-minute history is larger.
    while cur <= end:
        chunk_end = min(
            cur + timedelta(days=10),
            end
        )

        try:
            data = kite.historical_data(
                token,
                cur,
                chunk_end,
                "minute",
                continuous=False,
                oi=False,
            )
        except Exception as e:
            print(
                f"{save_name}: download error:",
                e
            )
            data = []

        rows.extend(data)

        cur = (
            chunk_end
            + timedelta(seconds=1)
        )

        time.sleep(0.4)

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
        index=False
    )

    return x


def trend(candles, signal_ts):
    if candles is None or candles.empty:
        return "UNKNOWN"

    x = candles.copy()

    ts_col = (
        "timestamp"
        if "timestamp" in x.columns
        else "date"
    )

    x[ts_col] = pd.to_datetime(
        x[ts_col]
    )

    ts = pd.Timestamp(signal_ts)

    candle_tz = x[ts_col].dt.tz

    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize(
                "Asia/Kolkata"
            )
        else:
            ts = ts.tz_convert(
                candle_tz
            )

    hist = x[
        x[ts_col] <= ts
    ].copy()

    if len(hist) < EMA_SLOW:
        return "UNKNOWN"

    close = pd.to_numeric(
        hist["close"],
        errors="coerce"
    )

    fast = close.ewm(
        span=EMA_FAST,
        adjust=False
    ).mean().iloc[-1]

    slow = close.ewm(
        span=EMA_SLOW,
        adjust=False
    ).mean().iloc[-1]

    last = float(close.iloc[-1])

    if (
        last <= 0
        or pd.isna(fast)
        or pd.isna(slow)
    ):
        return "UNKNOWN"

    spread = (
        (float(fast)-float(slow))
        / last
        * 100
    )

    if spread >= MIN_SPREAD_PCT:
        return "BULLISH"

    if spread <= -MIN_SPREAD_PCT:
        return "BEARISH"

    return "SIDEWAYS"


def choose_exact(train, t, prefix):
    g = train[
        (train[f"{prefix}_market"] == t[f"{prefix}_market"])
        &
        (train[f"{prefix}_sector"] == t[f"{prefix}_sector"])
        &
        (train["stock_trend"] == t["stock_trend"])
        &
        (train["direction"] == t["direction"])
    ]

    if len(g) < MIN_EXACT:
        return "SKIP"

    nn = float(
        g["normal_net"].sum()
    )

    rn = float(
        g["reverse_net"].sum()
    )

    if abs(nn-rn) < MIN_NET_ADVANTAGE:
        return "SKIP"

    if nn > 0 and nn > rn:
        return "NORMAL"

    if rn > 0 and rn > nn:
        return "REVERSE"

    return "SKIP"


def run_walkforward(df, prefix):
    rows = []

    dates = sorted(
        d for d in df["date"].unique()
        if d > INITIAL_TRAIN_END
    )

    for d in dates:

        train = df[
            df["date"] < d
        ]

        today = df[
            df["date"] == d
        ]

        for _, t in today.iterrows():

            decision = choose_exact(
                train,
                t,
                prefix
            )

            if decision == "SKIP":
                continue

            if decision == "NORMAL":
                gross = float(t["normal_gross"])
                costs = float(t["normal_costs"])
                net = float(t["normal_net"])
            else:
                gross = float(t["reverse_gross"])
                costs = float(t["reverse_costs"])
                net = float(t["reverse_net"])

            rows.append({
                "date": d,
                "signal_ts": t["signal_ts"],
                "symbol": t["symbol"],
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
            "win_rate": 0.0,
            "gross": 0.0,
            "costs": 0.0,
            "net": 0.0,
            "avg_net": 0.0,
            "max_drawdown": 0.0,
        }

    x = x.sort_values(
        ["date", "signal_ts"]
    )

    wins = int(
        (x["net"] > 0).sum()
    )

    eq = x["net"].cumsum()
    peak = eq.cummax()

    dd = (
        eq - peak
    ).min()

    return {
        "trades": len(x),
        "wins": wins,
        "losses": len(x)-wins,
        "win_rate":
            wins/len(x)*100,
        "gross":
            float(x["gross"].sum()),
        "costs":
            float(x["costs"].sum()),
        "net":
            float(x["net"].sum()),
        "avg_net":
            float(x["net"].mean()),
        "max_drawdown":
            float(dd),
    }


# --------------------------------------------------
# LOAD EXISTING FEATURES
# --------------------------------------------------

df = pd.read_csv(BASE)

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce"
).dt.strftime("%Y-%m-%d")

df["signal_ts"] = pd.to_datetime(
    df["signal_ts"],
    errors="coerce"
)

df = df[
    df["date"] != EXCLUDED_DATE
].copy()


# Existing 3m values
df["m3_market"] = df["market_trend"]
df["m3_sector"] = df["sector_trend"]


# --------------------------------------------------
# DOWNLOAD 1m MARKET / SECTOR
# --------------------------------------------------

kite = get_kite_client()

resolved = resolve_indices(kite)

print(
    "===== 1-MIN INDEX RESOLUTION ====="
)

for name in INDEXES:
    print(
        name,
        "OK" if name in resolved else "NOT FOUND"
    )

cache = {}

for name, token in resolved.items():

    print(
        "Downloading/loading:",
        name
    )

    cache[name] = download_1m(
        kite,
        token,
        name
    )

    print(
        " rows:",
        len(cache[name])
    )


# --------------------------------------------------
# CALCULATE NEW 1m MARKET / SECTOR
# --------------------------------------------------

m1_market = []
m1_sector = []

for _, r in df.iterrows():

    mt = trend(
        cache.get("NIFTY50"),
        r["signal_ts"]
    )

    index_name = SECTOR_INDEX.get(
        r["sector"]
    )

    if index_name:
        st = trend(
            cache.get(index_name),
            r["signal_ts"]
        )
    else:
        st = "UNKNOWN"

    m1_market.append(mt)
    m1_sector.append(st)

df["m1_market"] = m1_market
df["m1_sector"] = m1_sector


# --------------------------------------------------
# RUN FAIR WALK-FORWARD COMPARISON
# --------------------------------------------------

three = run_walkforward(
    df,
    "m3"
)

one = run_walkforward(
    df,
    "m1"
)

m3 = metrics(three)
m1 = metrics(one)

summary = pd.DataFrame([
    {
        "setup":
            "3m market + 3m sector + 3m stock",
        **m3
    },
    {
        "setup":
            "1m market + 1m sector + 3m stock",
        **m1
    },
])

print(
    "\n===== FINAL COMPARISON ====="
)

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate":
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

for label, x in [
    ("3M_3M_3M", three),
    ("1M_1M_3M", one),
]:

    if x.empty:
        continue

    d = (
        x.groupby("date")
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

    d["win_rate"] = (
        d["wins"]
        /
        d["trades"]
        * 100
    )

    d["cumulative_net"] = (
        d["net"].cumsum()
    )

    print(
        f"\n===== {label} DAYWISE ====="
    )

    print(
        d.to_string(
            index=False,
            formatters={
                "win_rate":
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

    d.to_csv(
        OUT / f"{label.lower()}_daywise.csv",
        index=False
    )


# --------------------------------------------------
# HOW OFTEN 1m DISAGREES WITH 3m
# --------------------------------------------------

print(
    "\n===== TREND DIFFERENCE ====="
)

print(
    "Market changed:",
    int(
        (
            df["m1_market"]
            !=
            df["m3_market"]
        ).sum()
    ),
    "/",
    len(df)
)

print(
    "Sector changed:",
    int(
        (
            df["m1_sector"]
            !=
            df["m3_sector"]
        ).sum()
    ),
    "/",
    len(df)
)

df.to_csv(
    OUT / "feature_comparison.csv",
    index=False
)

three.to_csv(
    OUT / "trades_3m_3m_3m.csv",
    index=False
)

one.to_csv(
    OUT / "trades_1m_1m_3m.csv",
    index=False
)

summary.to_csv(
    OUT / "summary.csv",
    index=False
)

print(
    "\nWrote:",
    OUT
)
