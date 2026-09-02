from pathlib import Path
import math
import numpy as np
import pandas as pd

EXCLUDED_DATE = "2026-07-30"

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test/"
    "base_normal_reverse.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_direction_test"
)
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# SECTOR MAP
# Add missing symbols after the first run if needed.
# --------------------------------------------------

SECTOR_MAP = {
    # BANK / FINANCIAL
    "ICICIBANK": "BANK",
    "BANKINDIA": "BANK",
    "UNIONBANK": "BANK",
    "J&KBANK": "BANK",
    "MANAPPURAM": "FINANCIAL",
    "M&MFIN": "FINANCIAL",
    "BAJAJFINSV": "FINANCIAL",
    "NUVAMA": "FINANCIAL",

    # IT
    "COFORGE": "IT",
    "FSL": "IT",
    "HFCL": "IT",

    # AUTO / AUTO ANCILLARY
    "TMCV": "AUTO",
    "BALKRISIND": "AUTO",
    "CIEINDIA": "AUTO",
    "ESCORTS": "AUTO",
    "BAJAJ-AUTO": "AUTO",
    "ENDURANCE": "AUTO",
    "MOTHERSON": "AUTO",
    "FORCEMOT": "AUTO",

    # METALS / MATERIALS
    "HINDCOPPER": "METAL",
    "JINDALSTEL": "METAL",
    "JSL": "METAL",
    "HINDALCO": "METAL",
    "GRAVITA": "METAL",

    # CONSUMER / FMCG / RETAIL
    "ITC": "FMCG",
    "SWIGGY": "CONSUMER",
    "MEESHO": "CONSUMER",
    "ABFRL": "CONSUMER",
    "HONASA": "FMCG",
    "PIDILITIND": "FMCG",

    # PHARMA / HEALTHCARE
    "IPCALAB": "PHARMA",
    "DRREDDY": "PHARMA",
    "ASTERDM": "HEALTHCARE",

    # INFRA / INDUSTRIAL
    "NCC": "INFRA",
    "TEGA": "INDUSTRIAL",
    "DYCL": "INDUSTRIAL",
    "JAINREC": "INDUSTRIAL",
    "CASTROLIND": "ENERGY",

    # Add more after unmapped report
}

# --------------------------------------------------
# POSSIBLE INDEX FILES
# Adjust names after first run if necessary.
# --------------------------------------------------

INDEX_SYMBOL_CANDIDATES = {
    "MARKET": [
        "NIFTY 50",
        "NIFTY50",
        "NIFTY",
    ],
    "BANK": [
        "NIFTY BANK",
        "BANKNIFTY",
        "NIFTYBANK",
    ],
    "FINANCIAL": [
        "NIFTY FIN SERVICE",
        "NIFTYFINSERVICE",
    ],
    "IT": [
        "NIFTY IT",
        "NIFTYIT",
    ],
    "AUTO": [
        "NIFTY AUTO",
        "NIFTYAUTO",
    ],
    "METAL": [
        "NIFTY METAL",
        "NIFTYMETAL",
    ],
    "FMCG": [
        "NIFTY FMCG",
        "NIFTYFMCG",
    ],
    "PHARMA": [
        "NIFTY PHARMA",
        "NIFTYPHARMA",
    ],
    "HEALTHCARE": [
        "NIFTY HEALTHCARE",
        "NIFTYHEALTHCARE",
    ],
    "ENERGY": [
        "NIFTY ENERGY",
        "NIFTYENERGY",
    ],
    "CONSUMER": [
        "NIFTY CONSUMER DURABLES",
        "NIFTYCONSUMER",
    ],
    "INFRA": [
        "NIFTY INFRASTRUCTURE",
        "NIFTYINFRA",
    ],
    "INDUSTRIAL": [
        "NIFTY INDIA MANUFACTURING",
        "NIFTYINDIAMFG",
    ],
}

# --------------------------------------------------
# TREND SETTINGS
# --------------------------------------------------

EMA_FAST = 9
EMA_SLOW = 21

# Avoid treating tiny differences as a directional trend.
MIN_EMA_SPREAD_PCT = 0.05

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def ema(s, n):
    return s.ewm(
        span=n,
        adjust=False
    ).mean()


def load_symbol_file(symbol):
    variants = [
        symbol,
        symbol.replace(" ", ""),
        symbol.replace(" ", "_"),
        symbol.replace("-", ""),
    ]

    for v in variants:
        p = CANDLE_DIR / f"{v}.parquet"
        if p.exists():
            x = pd.read_parquet(p)
            x["timestamp"] = pd.to_datetime(
                x["timestamp"]
            )
            return x, p

    return None, None


def resolve_index(kind):
    for candidate in INDEX_SYMBOL_CANDIDATES.get(kind, []):
        x, p = load_symbol_file(candidate)
        if x is not None:
            return candidate, x, p

    return None, None, None


def trend_at_signal(candles, signal_ts):
    if candles is None or candles.empty:
        return "UNKNOWN"

    ts = pd.Timestamp(signal_ts)

    candle_tz = candles["timestamp"].dt.tz

    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert(candle_tz)

    hist = candles[
        candles["timestamp"] <= ts
    ].copy()

    if len(hist) < EMA_SLOW:
        return "UNKNOWN"

    hist = hist.sort_values("timestamp")

    hist["ema_fast"] = ema(
        hist["close"],
        EMA_FAST
    )

    hist["ema_slow"] = ema(
        hist["close"],
        EMA_SLOW
    )

    r = hist.iloc[-1]

    fast = float(r["ema_fast"])
    slow = float(r["ema_slow"])
    close = float(r["close"])

    if close <= 0:
        return "UNKNOWN"

    spread_pct = (
        (fast - slow)
        / close
        * 100.0
    )

    if spread_pct >= MIN_EMA_SPREAD_PCT:
        return "BULLISH"

    if spread_pct <= -MIN_EMA_SPREAD_PCT:
        return "BEARISH"

    return "SIDEWAYS"


def max_drawdown(values):
    eq = 0.0
    peak = 0.0
    worst = 0.0

    for v in values:
        eq += float(v)
        peak = max(peak, eq)
        worst = min(
            worst,
            eq - peak
        )

    return worst


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
            "profit_factor": np.nan,
            "max_drawdown": 0.0,
        }

    wins = x[x["net"] > 0]
    losses = x[x["net"] <= 0]

    gp = float(
        x.loc[
            x["gross"] > 0,
            "gross"
        ].sum()
    )

    gl = float(
        x.loc[
            x["gross"] < 0,
            "gross"
        ].sum()
    )

    pf = (
        gp / abs(gl)
        if gl < 0
        else np.inf
    )

    return {
        "trades": len(x),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(x) * 100.0,
        "gross":
            float(x["gross"].sum()),
        "costs":
            float(x["costs"].sum()),
        "net":
            float(x["net"].sum()),
        "avg_net":
            float(x["net"].mean()),
        "profit_factor": pf,
        "max_drawdown":
            max_drawdown(
                x["net"].tolist()
            ),
    }


def outcome_row(t, mode):
    if mode == "NORMAL":
        return {
            "gross":
                float(t["normal_gross"]),
            "costs":
                float(t["normal_costs"]),
            "net":
                float(t["normal_net"]),
            "executed_direction":
                str(t["direction"]).upper(),
            "execution_mode":
                "NORMAL",
        }

    return {
        "gross":
            float(t["reverse_gross"]),
        "costs":
            float(t["reverse_costs"]),
        "net":
            float(t["reverse_net"]),
        "executed_direction":
            str(t["reverse_direction"]).upper(),
        "execution_mode":
            "REVERSE",
    }


# --------------------------------------------------
# LOAD BASE
# --------------------------------------------------

if not BASE.exists():
    raise SystemExit(f"Missing: {BASE}")

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

df = df.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

print("===== SOURCE =====")
print("Rows:", len(df))
print("Dates:", df["date"].nunique())
print("Symbols:", df["symbol"].nunique())

# --------------------------------------------------
# RESOLVE AVAILABLE INDEX FILES
# --------------------------------------------------

resolved = {}

print("\n===== INDEX FILE RESOLUTION =====")

for kind in INDEX_SYMBOL_CANDIDATES:
    name, data, path = resolve_index(kind)

    resolved[kind] = data

    if data is None:
        print(
            f"{kind:12s}: NOT FOUND"
        )
    else:
        print(
            f"{kind:12s}: {name} -> {path}"
        )

# --------------------------------------------------
# CLASSIFY TRADES
# --------------------------------------------------

rows = []
unmapped = set()

for _, t in df.iterrows():

    symbol = str(t["symbol"])
    original_direction = (
        str(t["direction"]).upper()
    )

    sector = SECTOR_MAP.get(symbol)

    if sector is None:
        unmapped.add(symbol)
        sector = "UNKNOWN"

    market_trend = trend_at_signal(
        resolved.get("MARKET"),
        t["signal_ts"]
    )

    sector_trend = trend_at_signal(
        resolved.get(sector),
        t["signal_ts"]
    )

    # ----------------------------------------------
    # Strategy 1:
    # Market + sector agreement decides direction.
    # ----------------------------------------------

    agreed_direction = None

    if (
        market_trend == "BULLISH"
        and
        sector_trend == "BULLISH"
    ):
        agreed_direction = "BUY"

    elif (
        market_trend == "BEARISH"
        and
        sector_trend == "BEARISH"
    ):
        agreed_direction = "SELL"

    # ----------------------------------------------
    # Strategy 2:
    # If original agrees with market+sector -> NORMAL.
    # If original disagrees -> REVERSE.
    # Else SKIP.
    # ----------------------------------------------

    if agreed_direction is None:
        action = "SKIP"
    elif original_direction == agreed_direction:
        action = "NORMAL"
    else:
        action = "REVERSE"

    base_info = {
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "symbol":
            symbol,
        "sector":
            sector,
        "market_trend":
            market_trend,
        "sector_trend":
            sector_trend,
        "original_direction":
            original_direction,
        "market_sector_direction":
            agreed_direction,
        "action":
            action,
    }

    # Always save normal/reverse alternatives too.
    n = outcome_row(
        t,
        "NORMAL"
    )

    r = outcome_row(
        t,
        "REVERSE"
    )

    base_info.update({
        "normal_net":
            n["net"],
        "reverse_net":
            r["net"],
    })

    if action == "SKIP":
        base_info.update({
            "executed_direction":
                None,
            "execution_mode":
                "SKIP",
            "gross":
                0.0,
            "costs":
                0.0,
            "net":
                0.0,
        })

    elif action == "NORMAL":
        base_info.update(n)

    else:
        base_info.update(r)

    rows.append(base_info)

result = pd.DataFrame(rows)

result.to_csv(
    OUT / "trade_level.csv",
    index=False
)

# --------------------------------------------------
# COVERAGE
# --------------------------------------------------

print("\n===== SECTOR MAPPING COVERAGE =====")

print(
    result["sector"]
    .value_counts()
    .to_string()
)

print("\nUnmapped symbols:", len(unmapped))

if unmapped:
    print(
        ", ".join(
            sorted(unmapped)
        )
    )

print("\n===== TREND COVERAGE =====")

print("Market:")
print(
    result["market_trend"]
    .value_counts()
    .to_string()
)

print("\nSector:")
print(
    result["sector_trend"]
    .value_counts()
    .to_string()
)

# --------------------------------------------------
# COMBINATION ANALYSIS
# --------------------------------------------------

combo = (
    result.groupby(
        [
            "market_trend",
            "sector_trend",
            "original_direction",
        ],
        dropna=False,
    )
    .agg(
        trades=("symbol", "size"),
        normal_wins=(
            "normal_net",
            lambda s: int((s > 0).sum())
        ),
        normal_net=(
            "normal_net",
            "sum"
        ),
        reverse_wins=(
            "reverse_net",
            lambda s: int((s > 0).sum())
        ),
        reverse_net=(
            "reverse_net",
            "sum"
        ),
    )
    .reset_index()
)

combo["normal_win_rate_pct"] = (
    combo["normal_wins"]
    /
    combo["trades"]
    * 100
)

combo["reverse_win_rate_pct"] = (
    combo["reverse_wins"]
    /
    combo["trades"]
    * 100
)

combo["better_side"] = np.where(
    combo["reverse_net"]
    >
    combo["normal_net"],
    "REVERSE",
    "NORMAL"
)

combo.to_csv(
    OUT / "market_sector_combo.csv",
    index=False
)

print(
    "\n===== MARKET x SECTOR x ORIGINAL DIRECTION ====="
)

print(
    combo.to_string(
        index=False,
        formatters={
            "normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "normal_win_rate_pct":
                lambda v:
                    f"{v:.1f}%",
            "reverse_win_rate_pct":
                lambda v:
                    f"{v:.1f}%",
        }
    )
)

# --------------------------------------------------
# STRATEGY RESULTS
# --------------------------------------------------

taken = result[
    result["action"] != "SKIP"
].copy()

m = metrics(taken)

print(
    "\n===== MARKET + SECTOR DIRECTION STRATEGY ====="
)

for k, v in m.items():
    if isinstance(v, float):
        print(f"{k:20s}: {v:.2f}")
    else:
        print(f"{k:20s}: {v}")

print(
    "\nNormal trades :",
    int(
        (
            taken["execution_mode"]
            ==
            "NORMAL"
        ).sum()
    )
)

print(
    "Reverse trades:",
    int(
        (
            taken["execution_mode"]
            ==
            "REVERSE"
        ).sum()
    )
)

print(
    "Skipped trades:",
    int(
        (
            result["action"]
            ==
            "SKIP"
        ).sum()
    )
)

# --------------------------------------------------
# DAYWISE
# --------------------------------------------------

day = (
    taken.groupby("date")
    .agg(
        trades=("symbol", "size"),
        normal_trades=(
            "execution_mode",
            lambda s:
                int((s == "NORMAL").sum())
        ),
        reverse_trades=(
            "execution_mode",
            lambda s:
                int((s == "REVERSE").sum())
        ),
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
        gross=("gross", "sum"),
        costs=("costs", "sum"),
        net=("net", "sum"),
    )
    .reset_index()
)

day["cumulative_net"] = (
    day["net"].cumsum()
)

day.to_csv(
    OUT / "daywise.csv",
    index=False
)

print(
    "\n===== DAYWISE ====="
)

print(
    day.to_string(
        index=False,
        formatters={
            "gross":
                lambda v:
                    f"Rs {v:,.2f}",
            "costs":
                lambda v:
                    f"Rs {v:,.2f}",
            "net":
                lambda v:
                    f"Rs {v:,.2f}",
            "cumulative_net":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)

# --------------------------------------------------
# SECTOR PERFORMANCE
# --------------------------------------------------

sector_perf = (
    result.groupby("sector")
    .agg(
        trades=("symbol", "size"),
        normal_net=("normal_net", "sum"),
        reverse_net=("reverse_net", "sum"),
    )
    .reset_index()
)

sector_perf[
    "preferred_history"
] = np.where(
    sector_perf["reverse_net"]
    >
    sector_perf["normal_net"],
    "REVERSE",
    "NORMAL"
)

sector_perf.to_csv(
    OUT / "sector_performance.csv",
    index=False
)

print(
    "\n===== SECTOR NORMAL VS REVERSE ====="
)

print(
    sector_perf.sort_values(
        "reverse_net",
        ascending=False
    ).to_string(
        index=False,
        formatters={
            "normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)

print(
    "\nWrote:",
    OUT
)
