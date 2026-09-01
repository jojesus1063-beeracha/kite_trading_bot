from pathlib import Path
import pandas as pd
import numpy as np

TRADES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/trades_3m.csv"
)

FEATURES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/feature_comparison.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "ema3_corrected_replay"
)

OUT.mkdir(parents=True, exist_ok=True)

MAX_WAIT_CANDLES = 2


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


# ------------------------------------------------------------
# LOAD TRADES
# ------------------------------------------------------------

trades = pd.read_csv(TRADES)
features = pd.read_csv(FEATURES)

for df in [trades, features]:

    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    df["signal_ts"] = pd.to_datetime(
        df["signal_ts"],
        errors="coerce"
    )

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

for c in ["gross", "costs", "net"]:
    trades[c] = pd.to_numeric(
        trades[c],
        errors="coerce"
    ).fillna(0)


# ------------------------------------------------------------
# GET ORIGINAL BUY/SELL DIRECTION
# ------------------------------------------------------------

direction_cols = [
    "date",
    "signal_ts",
    "symbol",
    "direction",
    "reverse_direction",
]

direction_cols = [
    c for c in direction_cols
    if c in features.columns
]

f = features[direction_cols].copy()

f = f.drop_duplicates(
    ["date", "signal_ts", "symbol"]
)

trades = trades.merge(
    f,
    on=["date", "signal_ts", "symbol"],
    how="left"
)


def executed_side(row):

    decision = str(
        row["decision"]
    ).upper()

    normal = str(
        row.get("direction", "")
    ).upper()

    reverse = str(
        row.get("reverse_direction", "")
    ).upper()

    if decision == "NORMAL":
        return normal

    if decision == "REVERSE":
        return reverse

    return None


trades["executed_side"] = trades.apply(
    executed_side,
    axis=1
)


# ------------------------------------------------------------
# FIND CANDLES
# ------------------------------------------------------------

def read_candle_file(path):

    try:

        if path.suffix.lower() == ".parquet":
            x = pd.read_parquet(path)
        else:
            x = pd.read_csv(path)

    except Exception:
        return None

    if x.empty:
        return None

    x.columns = [
        str(c).lower().strip()
        for c in x.columns
    ]

    ts_col = None

    for c in [
        "timestamp",
        "datetime",
        "date",
        "time"
    ]:
        if c in x.columns:
            ts_col = c
            break

    if ts_col is None:
        return None

    if "close" not in x.columns:
        return None

    x["timestamp"] = pd.to_datetime(
        x[ts_col],
        errors="coerce"
    )

    x["close"] = pd.to_numeric(
        x["close"],
        errors="coerce"
    )

    x = x.dropna(
        subset=["timestamp", "close"]
    )

    return x


def load_day(symbol, date):

    # First try filenames containing symbol/date.
    patterns = [
        f"**/*{date}*{symbol}*",
        f"**/*{symbol}*{date}*",
        f"**/*{date.replace('-', '')}*{symbol}*",
        f"**/*{symbol}*{date.replace('-', '')}*",
    ]

    candidates = []

    for pat in patterns:
        candidates.extend(
            CANDLE_DIR.glob(pat)
        )

    candidates = [
        p for p in set(candidates)
        if p.is_file()
        and p.suffix.lower()
        in {".csv", ".parquet"}
    ]

    for p in candidates:

        x = read_candle_file(p)

        if x is None:
            continue

        day = x[
            x["timestamp"]
            .dt.strftime("%Y-%m-%d")
            == date
        ].copy()

        if "symbol" in day.columns:

            day["symbol"] = (
                day["symbol"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            day = day[
                day["symbol"] == symbol
            ]

        if len(day):

            return (
                day
                .sort_values("timestamp")
                .drop_duplicates("timestamp")
                .reset_index(drop=True),
                str(p)
            )

    return None, None


# ------------------------------------------------------------
# EMA ALIGNMENT
# ------------------------------------------------------------

def aligned(side, e3, e9, e21):

    if side == "BUY":
        return (
            e3 > e9
            and e9 > e21
        )

    if side == "SELL":
        return (
            e3 < e9
            and e9 < e21
        )

    return False


# ------------------------------------------------------------
# REPLAY
# ------------------------------------------------------------

results = []

print()
print("=" * 115)
print("CORRECTED EMA3 / EMA9 / EMA21 TEST")
print("=" * 115)

for _, trade in trades.iterrows():

    symbol = trade["symbol"]
    date = trade["date"]
    ts = trade["signal_ts"]
    side = trade["executed_side"]

    result = trade.to_dict()

    result.update({
        "candle_source": None,
        "ema3": np.nan,
        "ema9": np.nan,
        "ema21": np.nan,
        "ema3_status": None,
        "alignment_ts": pd.NaT,
        "wait_candles": np.nan,
    })

    if side not in ["BUY", "SELL"]:

        result["ema3_status"] = "NO_SIDE"

        results.append(result)

        print(
            f"{date} {symbol:<12} "
            f"decision={trade['decision']:<7} "
            "NO_SIDE"
        )

        continue

    candles, source = load_day(
        symbol,
        date
    )

    if candles is None:

        result["ema3_status"] = "NO_CANDLES"

        results.append(result)

        print(
            f"{date} {symbol:<12} "
            f"{side:<4} NO_CANDLES"
        )

        continue

    result["candle_source"] = source

    candles["ema3"] = ema(
        candles["close"], 3
    )

    candles["ema9"] = ema(
        candles["close"], 9
    )

    candles["ema21"] = ema(
        candles["close"], 21
    )

    eligible = candles[
        candles["timestamp"] <= ts
    ]

    if eligible.empty:

        result["ema3_status"] = (
            "NO_SIGNAL_CANDLE"
        )

        results.append(result)
        continue

    idx = eligible.index[-1]

    sig = candles.loc[idx]

    result["ema3"] = sig["ema3"]
    result["ema9"] = sig["ema9"]
    result["ema21"] = sig["ema21"]

    if aligned(
        side,
        sig["ema3"],
        sig["ema9"],
        sig["ema21"]
    ):

        result["ema3_status"] = (
            "PASS_IMMEDIATE"
        )

        result["alignment_ts"] = (
            sig["timestamp"]
        )

        result["wait_candles"] = 0

    else:

        result["ema3_status"] = (
            "BLOCK_EMA3"
        )

        future = candles.loc[
            idx + 1:
            idx + MAX_WAIT_CANDLES
        ]

        for wait, (_, c) in enumerate(
            future.iterrows(),
            start=1
        ):

            if aligned(
                side,
                c["ema3"],
                c["ema9"],
                c["ema21"]
            ):

                result["ema3_status"] = (
                    "PASS_DELAYED"
                )

                result["alignment_ts"] = (
                    c["timestamp"]
                )

                result["wait_candles"] = wait

                break

    results.append(result)

    print(
        f"{date} "
        f"{symbol:<12} "
        f"{side:<4} "
        f"{result['ema3_status']:<15} "
        f"net={trade['net']:+.2f}"
    )


r = pd.DataFrame(results)

r["ema3_pass"] = r[
    "ema3_status"
].isin([
    "PASS_IMMEDIATE",
    "PASS_DELAYED"
])


# ------------------------------------------------------------
# VALID COMPARISONS ONLY
# ------------------------------------------------------------

known = r[
    r["ema3_status"].isin([
        "PASS_IMMEDIATE",
        "PASS_DELAYED",
        "BLOCK_EMA3"
    ])
].copy()

passed = known[
    known["ema3_pass"]
].copy()

blocked = known[
    ~known["ema3_pass"]
].copy()


print()
print("=" * 115)
print("BASELINE")
print("=" * 115)

print("Trades :", len(trades))
print(
    "Wins   :",
    (trades["net"] > 0).sum()
)
print(
    "Losses :",
    (trades["net"] < 0).sum()
)
print(
    f"NET    : Rs {trades['net'].sum():.2f}"
)


print()
print("=" * 115)
print("DATA COVERAGE")
print("=" * 115)

print(
    r["ema3_status"]
    .value_counts()
    .to_string()
)


print()
print("=" * 115)
print("EMA3 FILTER RESULT")
print("=" * 115)

print(
    "Comparable trades :",
    len(known)
)

print(
    "Passed            :",
    len(passed)
)

print(
    "Blocked           :",
    len(blocked)
)

print(
    "Immediate         :",
    (known["ema3_status"]
     == "PASS_IMMEDIATE").sum()
)

print(
    "Delayed           :",
    (known["ema3_status"]
     == "PASS_DELAYED").sum()
)


if len(passed):

    print(
        "Winners           :",
        (passed["net"] > 0).sum()
    )

    print(
        "Losers            :",
        (passed["net"] < 0).sum()
    )

    print(
        f"Original-PnL of survivors : "
        f"Rs {passed['net'].sum():.2f}"
    )


print()
print("=" * 115)
print("WINNERS BLOCKED")
print("=" * 115)

bw = blocked[
    blocked["net"] > 0
]

print(
    "Count :",
    len(bw)
)

print(
    f"P&L   : Rs {bw['net'].sum():.2f}"
)

if len(bw):

    print(
        bw[
            [
                "date",
                "signal_ts",
                "symbol",
                "executed_side",
                "net"
            ]
        ].to_string(index=False)
    )


print()
print("=" * 115)
print("LOSERS BLOCKED")
print("=" * 115)

bl = blocked[
    blocked["net"] < 0
]

print(
    "Count :",
    len(bl)
)

print(
    f"P&L   : Rs {bl['net'].sum():.2f}"
)

if len(bl):

    print(
        bl[
            [
                "date",
                "signal_ts",
                "symbol",
                "executed_side",
                "net"
            ]
        ].to_string(index=False)
    )


print()
print("=" * 115)
print("TRADE LEVEL")
print("=" * 115)

cols = [
    "date",
    "signal_ts",
    "symbol",
    "decision",
    "executed_side",
    "net",
    "ema3",
    "ema9",
    "ema21",
    "ema3_status",
    "alignment_ts",
    "wait_candles",
]

print(
    r[cols].to_string(index=False)
)


print()
print("=" * 115)
print("IMPORTANT")
print("=" * 115)

print(
    "PASS_DELAYED P&L still uses the original trade result."
)

print(
    "After this test we must replay delayed entries at their "
    "actual later entry price before calling it final P&L."
)


r.to_csv(
    OUT / "trade_level.csv",
    index=False
)

passed.to_csv(
    OUT / "passed.csv",
    index=False
)

blocked.to_csv(
    OUT / "blocked.csv",
    index=False
)

print()
print("Saved:", OUT)

