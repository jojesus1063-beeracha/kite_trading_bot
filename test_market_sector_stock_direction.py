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
    "market_sector_stock_direction_test"
)
OUT.mkdir(parents=True, exist_ok=True)

EMA_FAST = 9
EMA_SLOW = 21
MIN_EMA_SPREAD_PCT = 0.05

MIN_CELL_TRADES = 5

# --------------------------------------------------
# SECTOR INDEX FILES
# --------------------------------------------------

SECTOR_INDEX = {
    "BANK": "NIFTYBANK",
    "FINANCIAL": "NIFTYFINSERVICE",
    "IT": "NIFTYIT",
    "AUTO": "NIFTYAUTO",
    "METAL": "NIFTYMETAL",
    "FMCG": "NIFTYFMCG",
    "PHARMA": "NIFTYPHARMA",
    "HEALTHCARE": "NIFTYHEALTHCARE",
    "ENERGY": "NIFTYENERGY",
    "REALTY": "NIFTYREALTY",
    "PSUBANK": "NIFTYPSUBANK",
    "MEDIA": "NIFTYMEDIA",

    # No direct files available in previous download.
    # Map these to the closest relevant broad index only where sensible.
    "CONSUMER": None,
    "INFRA": None,
    "INDUSTRIAL": None,
}

# --------------------------------------------------
# COMPLETE SYMBOL -> SECTOR MAP
# For the 79 symbols in the current 142-trade sample.
# --------------------------------------------------

SECTOR_MAP = {
    # Financial / Banking
    "AAVAS": "FINANCIAL",
    "ANANDRATHI": "FINANCIAL",
    "BANDHANBNK": "BANK",
    "FEDERALBNK": "BANK",
    "ICICIBANK": "BANK",
    "ICICIGI": "FINANCIAL",
    "IFCI": "FINANCIAL",
    "J&KBANK": "BANK",
    "M&MFIN": "FINANCIAL",
    "MANAPPURAM": "FINANCIAL",
    "NUVAMA": "FINANCIAL",
    "POLICYBZR": "FINANCIAL",
    "RBLBANK": "BANK",
    "BAJAJFINSV": "FINANCIAL",
    "BANKINDIA": "PSUBANK",
    "UNIONBANK": "PSUBANK",

    # Auto / Auto ancillaries
    "ASHOKLEY": "AUTO",
    "BALUFORGE": "AUTO",
    "CIEINDIA": "AUTO",
    "DIVGIITTS": "AUTO",
    "ENDURANCE": "AUTO",
    "ESCORTS": "AUTO",
    "FORCEMOT": "AUTO",
    "GABRIEL": "AUTO",
    "MOTHERSON": "AUTO",
    "TMCV": "AUTO",
    "BALKRISIND": "AUTO",
    "BAJAJ-AUTO": "AUTO",

    # IT / Technology / Telecom tech
    "COFORGE": "IT",
    "FSL": "IT",
    "HFCL": "IT",

    # Metals / Mining
    "ALOKINDS": "METAL",
    "HINDCOPPER": "METAL",
    "HINDALCO": "METAL",
    "JINDALSTEL": "METAL",
    "JSL": "METAL",
    "NATIONALUM": "METAL",
    "SAIL": "METAL",
    "TATASTEEL": "METAL",
    "GRAVITA": "METAL",

    # Energy / Oil / Power
    "CASTROLIND": "ENERGY",
    "CHENNPETRO": "ENERGY",
    "GIPCL": "ENERGY",
    "GREENPOWER": "ENERGY",
    "INOXWIND": "ENERGY",
    "IREDA": "ENERGY",
    "NTPC": "ENERGY",
    "SPLPETRO": "ENERGY",

    # Pharma / Healthcare
    "AGARWALEYE": "HEALTHCARE",
    "ASTERDM": "HEALTHCARE",
    "INDSWFTLAB": "PHARMA",
    "IPCALAB": "PHARMA",
    "MARKSANS": "PHARMA",
    "TARSONS": "HEALTHCARE",
    "THYROCARE": "HEALTHCARE",
    "ZYDUSLIFE": "PHARMA",
    "DRREDDY": "PHARMA",

    # Realty
    "DLF": "REALTY",
    "GODREJPROP": "REALTY",

    # FMCG / Consumer
    "BIKAJI": "FMCG",
    "CCL": "FMCG",
    "ITC": "FMCG",
    "JUBLFOOD": "CONSUMER",
    "KALYANKJIL": "CONSUMER",
    "TITAN": "CONSUMER",
    "HONASA": "FMCG",
    "PIDILITIND": "FMCG",
    "SWIGGY": "CONSUMER",
    "MEESHO": "CONSUMER",
    "ITCHOTELS": "CONSUMER",

    # Media
    "ZEEL": "MEDIA",

    # Infra / Industrial / Engineering
    "AEQUS": "INDUSTRIAL",
    "ARDEE": "INDUSTRIAL",
    "BAJEL": "INFRA",
    "BLUEDART": "INDUSTRIAL",
    "CGCL": "INDUSTRIAL",
    "CGPOWER": "INDUSTRIAL",
    "CELLO": "INDUSTRIAL",
    "DICIND": "INDUSTRIAL",
    "EMIL": "INDUSTRIAL",
    "FINCABLES": "INDUSTRIAL",
    "IRCON": "INFRA",
    "JAINREC": "INDUSTRIAL",
    "JWL": "INDUSTRIAL",
    "KRONOX": "INDUSTRIAL",
    "NCC": "INFRA",
    "OMNI": "INDUSTRIAL",
    "POLYPLEX": "INDUSTRIAL",
    "SUDARSCHEM": "INDUSTRIAL",
    "TEGA": "INDUSTRIAL",
    "TRITURBINE": "INDUSTRIAL",
    "VOGL": "INDUSTRIAL",
    "DYCL": "INDUSTRIAL",
    "CUPID": "INDUSTRIAL",
    "COROMANDEL": "INDUSTRIAL",
    "MANORAMA": "INDUSTRIAL",
    "CHOLAHLDNG": "FINANCIAL",
}

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def ema(s, n):
    return s.ewm(
        span=n,
        adjust=False
    ).mean()


def load_file(symbol):
    p = CANDLE_DIR / f"{symbol}.parquet"

    if not p.exists():
        return None

    x = pd.read_parquet(p)

    x["timestamp"] = pd.to_datetime(
        x["timestamp"]
    )

    return (
        x.sort_values("timestamp")
        .reset_index(drop=True)
    )


CACHE = {}


def cached(symbol):
    if symbol not in CACHE:
        CACHE[symbol] = load_file(symbol)
    return CACHE[symbol]


def trend_at_signal(candles, signal_ts):
    if candles is None or candles.empty:
        return "UNKNOWN"

    ts = pd.Timestamp(signal_ts)

    candle_tz = candles["timestamp"].dt.tz

    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize(
                "Asia/Kolkata"
            )
        else:
            ts = ts.tz_convert(
                candle_tz
            )

    hist = candles[
        candles["timestamp"] <= ts
    ].copy()

    if len(hist) < EMA_SLOW:
        return "UNKNOWN"

    hist["ema_fast"] = ema(
        hist["close"],
        EMA_FAST
    )

    hist["ema_slow"] = ema(
        hist["close"],
        EMA_SLOW
    )

    r = hist.iloc[-1]

    close = float(r["close"])
    fast = float(r["ema_fast"])
    slow = float(r["ema_slow"])

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


def get_outcome(t, mode):
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
            "mode": "NORMAL",
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
        "mode": "REVERSE",
    }


# --------------------------------------------------
# LOAD SOURCE
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

df = df.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

print("===== SOURCE =====")
print("Rows   :", len(df))
print("Dates  :", df["date"].nunique())
print("Symbols:", df["symbol"].nunique())

# --------------------------------------------------
# MAP + TREND CALCULATION
# --------------------------------------------------

rows = []

for _, t in df.iterrows():

    symbol = str(t["symbol"])
    sector = SECTOR_MAP.get(
        symbol,
        "UNKNOWN"
    )

    market_trend = trend_at_signal(
        cached("NIFTY50"),
        t["signal_ts"]
    )

    sector_file = (
        SECTOR_INDEX.get(sector)
    )

    if sector_file:
        sector_trend = trend_at_signal(
            cached(sector_file),
            t["signal_ts"]
        )
    else:
        sector_trend = "UNKNOWN"

    stock_trend = trend_at_signal(
        cached(symbol),
        t["signal_ts"]
    )

    rows.append({
        **t.to_dict(),
        "sector":
            sector,
        "market_trend":
            market_trend,
        "sector_trend":
            sector_trend,
        "stock_trend":
            stock_trend,
    })

x = pd.DataFrame(rows)

x.to_csv(
    OUT / "feature_level.csv",
    index=False
)

print(
    "\n===== SECTOR COVERAGE ====="
)

print(
    x["sector"]
    .value_counts()
    .to_string()
)

print(
    "\nUNKNOWN sector:",
    int(
        (x["sector"] == "UNKNOWN").sum()
    )
)

print(
    "\n===== TREND COVERAGE ====="
)

for c in [
    "market_trend",
    "sector_trend",
    "stock_trend",
]:
    print(f"\n{c}")
    print(
        x[c].value_counts().to_string()
    )

# --------------------------------------------------
# COMPLETE COMBINATION MATRIX
# --------------------------------------------------

matrix_rows = []

group_cols = [
    "market_trend",
    "sector_trend",
    "stock_trend",
    "direction",
]

for keys, g in x.groupby(
    group_cols,
    dropna=False
):

    market, sector, stock, direction = keys

    normal_wins = int(
        (g["normal_net"] > 0).sum()
    )

    reverse_wins = int(
        (g["reverse_net"] > 0).sum()
    )

    normal_net = float(
        g["normal_net"].sum()
    )

    reverse_net = float(
        g["reverse_net"].sum()
    )

    trades = len(g)

    better = (
        "NORMAL"
        if normal_net >= reverse_net
        else "REVERSE"
    )

    matrix_rows.append({
        "market_trend": market,
        "sector_trend": sector,
        "stock_trend": stock,
        "original_direction": direction,
        "trades": trades,

        "normal_wins": normal_wins,
        "normal_losses":
            trades-normal_wins,
        "normal_win_rate_pct":
            normal_wins/trades*100,
        "normal_net":
            normal_net,

        "reverse_wins":
            reverse_wins,
        "reverse_losses":
            trades-reverse_wins,
        "reverse_win_rate_pct":
            reverse_wins/trades*100,
        "reverse_net":
            reverse_net,

        "better_history":
            better,

        "net_advantage":
            abs(
                normal_net
                -
                reverse_net
            ),
    })

matrix = pd.DataFrame(
    matrix_rows
).sort_values(
    ["trades", "net_advantage"],
    ascending=[False, False]
)

matrix.to_csv(
    OUT / "combination_matrix.csv",
    index=False
)

print(
    "\n===== MARKET x SECTOR x STOCK x SIGNAL ====="
)

print(
    matrix.to_string(
        index=False,
        formatters={
            "normal_win_rate_pct":
                lambda v: f"{v:.1f}%",
            "reverse_win_rate_pct":
                lambda v: f"{v:.1f}%",
            "normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "net_advantage":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)

# --------------------------------------------------
# LEARN DECISION TABLE
#
# IMPORTANT:
# This is in-sample descriptive research only.
#
# >= MIN_CELL_TRADES:
# choose historical better side if clearly profitable.
#
# Otherwise SKIP.
# --------------------------------------------------

decision_rows = []

for _, r in matrix.iterrows():

    decision = "SKIP"

    if int(r["trades"]) >= MIN_CELL_TRADES:

        if (
            r["normal_net"] > 0
            and
            r["normal_net"]
            >=
            r["reverse_net"]
        ):
            decision = "NORMAL"

        elif (
            r["reverse_net"] > 0
            and
            r["reverse_net"]
            >
            r["normal_net"]
        ):
            decision = "REVERSE"

    decision_rows.append({
        **r.to_dict(),
        "decision": decision,
    })

decision_table = pd.DataFrame(
    decision_rows
)

decision_table.to_csv(
    OUT / "decision_table.csv",
    index=False
)

print(
    "\n===== LEARNED DECISION TABLE ====="
)

print(
    decision_table[
        [
            "market_trend",
            "sector_trend",
            "stock_trend",
            "original_direction",
            "trades",
            "normal_net",
            "reverse_net",
            "decision",
        ]
    ].to_string(
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

# --------------------------------------------------
# APPLY DESCRIPTIVE DECISION TABLE
# --------------------------------------------------

lookup = {}

for _, r in decision_table.iterrows():

    key = (
        r["market_trend"],
        r["sector_trend"],
        r["stock_trend"],
        r["original_direction"],
    )

    lookup[key] = r["decision"]

executed_rows = []

for _, t in x.iterrows():

    key = (
        t["market_trend"],
        t["sector_trend"],
        t["stock_trend"],
        t["direction"],
    )

    decision = lookup.get(
        key,
        "SKIP"
    )

    if decision == "SKIP":
        continue

    o = get_outcome(
        t,
        decision
    )

    executed_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "symbol":
            t["symbol"],
        "sector":
            t["sector"],
        "market_trend":
            t["market_trend"],
        "sector_trend":
            t["sector_trend"],
        "stock_trend":
            t["stock_trend"],
        "original_direction":
            t["direction"],
        "decision":
            decision,
        **o,
    })

executed = pd.DataFrame(
    executed_rows
)

executed.to_csv(
    OUT / "executed_trade_level.csv",
    index=False
)

m = metrics(executed)

print(
    "\n===== DESCRIPTIVE DECISION-TABLE RESULT ====="
)

for k, v in m.items():

    if isinstance(v, float):
        print(
            f"{k:20s}: {v:.2f}"
        )
    else:
        print(
            f"{k:20s}: {v}"
        )

print(
    "Skipped:",
    len(x)-len(executed)
)

# --------------------------------------------------
# DAYWISE
# --------------------------------------------------

if not executed.empty:

    day = (
        executed.groupby("date")
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
            normal_trades=(
                "decision",
                lambda s:
                    int((s == "NORMAL").sum())
            ),
            reverse_trades=(
                "decision",
                lambda s:
                    int((s == "REVERSE").sum())
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

else:

    day = pd.DataFrame()

day.to_csv(
    OUT / "daywise.csv",
    index=False
)

print(
    "\n===== DAYWISE ====="
)

if day.empty:
    print("None")
else:
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
# SECTOR x ORIGINAL DIRECTION
# --------------------------------------------------

sector_rows = []

for keys, g in x.groupby(
    ["sector", "direction"]
):

    sector, direction = keys

    sector_rows.append({
        "sector": sector,
        "direction": direction,
        "trades": len(g),
        "normal_wins":
            int(
                (g["normal_net"] > 0).sum()
            ),
        "normal_net":
            g["normal_net"].sum(),
        "reverse_wins":
            int(
                (g["reverse_net"] > 0).sum()
            ),
        "reverse_net":
            g["reverse_net"].sum(),
    })

sector_perf = pd.DataFrame(
    sector_rows
)

sector_perf["normal_wr"] = (
    sector_perf["normal_wins"]
    /
    sector_perf["trades"]
    * 100
)

sector_perf["reverse_wr"] = (
    sector_perf["reverse_wins"]
    /
    sector_perf["trades"]
    * 100
)

sector_perf[
    "better_history"
] = np.where(
    sector_perf["reverse_net"]
    >
    sector_perf["normal_net"],
    "REVERSE",
    "NORMAL"
)

sector_perf.to_csv(
    OUT / "sector_direction_performance.csv",
    index=False
)

print(
    "\n===== SECTOR x ORIGINAL DIRECTION ====="
)

print(
    sector_perf.sort_values(
        ["sector", "direction"]
    ).to_string(
        index=False,
        formatters={
            "normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "normal_wr":
                lambda v:
                    f"{v:.1f}%",
            "reverse_wr":
                lambda v:
                    f"{v:.1f}%",
        }
    )
)

print(
    "\nWrote:",
    OUT
)
