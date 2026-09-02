from pathlib import Path
import pandas as pd
import numpy as np

# ============================================================
# CONFIG
# ============================================================

TRADES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/trades_3m.csv"
)

# Search locations for historical candles
CANDLE_DIRS = [
    Path("runtime"),
    Path("runtime/candle_cache"),
    Path("runtime/historical_candles"),
    Path("runtime/watchlist_missed_opportunity"),
]

# How long EMA3 is allowed to wait for EMA9 alignment.
# 3-minute candles:
# 1 candle = 3 mins
# 2 candles = 6 mins
# 3 candles = 9 mins
MAX_WAIT_CANDLES = 2

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "ema3_proposed_logic_replay"
)
OUT.mkdir(parents=True, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def normalize_direction(decision):
    d = str(decision).upper().strip()

    # IMPORTANT:
    # trades_3m has NORMAL/REVERSE, not necessarily BUY/SELL.
    # We cannot infer actual side purely from NORMAL/REVERSE.
    return d


def load_candidate_candles(symbol, date):
    """
    Find historical candle files containing symbol/date.
    Supports parquet/csv files.
    """

    candidates = []

    symbol_upper = symbol.upper()

    for root in CANDLE_DIRS:
        if not root.exists():
            continue

        patterns = [
            f"**/*{date}*{symbol}*.parquet",
            f"**/*{symbol}*{date}*.parquet",
            f"**/*{date}*{symbol}*.csv",
            f"**/*{symbol}*{date}*.csv",
        ]

        for pattern in patterns:
            try:
                candidates.extend(root.glob(pattern))
            except Exception:
                pass

    # remove duplicates
    seen = set()
    unique = []

    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique.append(p)

    for p in unique:
        try:
            if p.suffix.lower() == ".parquet":
                df = pd.read_parquet(p)
            else:
                df = pd.read_csv(p)

            if df.empty:
                continue

            # normalize columns
            df.columns = [
                str(c).lower().strip()
                for c in df.columns
            ]

            # timestamp
            ts_col = None

            for c in [
                "timestamp",
                "datetime",
                "date",
                "time"
            ]:
                if c in df.columns:
                    ts_col = c
                    break

            if ts_col is None:
                continue

            if "close" not in df.columns:
                continue

            df["timestamp"] = pd.to_datetime(
                df[ts_col],
                errors="coerce"
            )

            df = df.dropna(
                subset=["timestamp", "close"]
            ).copy()

            if df.empty:
                continue

            # restrict date
            mask = (
                df["timestamp"]
                .dt.date
                .astype(str)
                == date
            )

            day = df.loc[mask].copy()

            if day.empty:
                continue

            # If symbol column exists, filter it
            if "symbol" in day.columns:
                day["symbol"] = (
                    day["symbol"]
                    .astype(str)
                    .str.upper()
                    .str.strip()
                )

                day = day[
                    day["symbol"] == symbol_upper
                ].copy()

            if day.empty:
                continue

            day = (
                day.sort_values("timestamp")
                .drop_duplicates("timestamp")
                .reset_index(drop=True)
            )

            return day, str(p)

        except Exception:
            continue

    return None, None


def infer_side(row):
    """
    Try to find actual BUY/SELL side from available trade columns.
    """

    for c in [
        "direction",
        "side",
        "trade_direction",
        "signal_direction",
        "original_direction",
        "counterfactual_direction"
    ]:
        if c in row.index:
            v = str(row[c]).upper().strip()

            if v in ("BUY", "SELL"):
                return v

    return None


def alignment(side, ema3, ema9, ema21):
    """
    Full 3/9/21 alignment.
    """

    if side == "BUY":
        return (
            ema3 > ema9
            and ema9 > ema21
        )

    if side == "SELL":
        return (
            ema3 < ema9
            and ema9 < ema21
        )

    return False


# ============================================================
# LOAD TRADES
# ============================================================

trades = pd.read_csv(TRADES)

trades["date"] = (
    pd.to_datetime(
        trades["date"],
        errors="coerce"
    )
    .dt.date
    .astype(str)
)

trades["symbol"] = (
    trades["symbol"]
    .astype(str)
    .str.upper()
    .str.strip()
)

trades["signal_ts"] = pd.to_datetime(
    trades["signal_ts"],
    errors="coerce"
)

for c in ["gross", "costs", "net"]:
    trades[c] = pd.to_numeric(
        trades[c],
        errors="coerce"
    ).fillna(0)


# ============================================================
# REPLAY
# ============================================================

results = []

print()
print("=" * 120)
print("EMA3 PROPOSED LOGIC REPLAY")
print("=" * 120)

for _, trade in trades.iterrows():

    date = trade["date"]
    symbol = trade["symbol"]
    signal_ts = trade["signal_ts"]

    side = infer_side(trade)

    candles, source = load_candidate_candles(
        symbol,
        date
    )

    result = trade.to_dict()

    result.update({
        "actual_side": side,
        "candle_source": source,
        "ema3_at_signal": np.nan,
        "ema9_at_signal": np.nan,
        "ema21_at_signal": np.nan,
        "ema3_aligned_at_signal": False,
        "ema3_eventually_aligned": False,
        "alignment_ts": pd.NaT,
        "wait_candles": np.nan,
        "ema3_status": None,
    })

    if candles is None:

        result["ema3_status"] = "NO_CANDLES"
        results.append(result)

        print(
            f"{date} {symbol:<12} "
            f"NO CANDLES"
        )

        continue

    if side is None:

        result["ema3_status"] = "NO_SIDE"
        results.append(result)

        print(
            f"{date} {symbol:<12} "
            f"NO BUY/SELL SIDE"
        )

        continue

    candles["ema3"] = ema(
        candles["close"],
        3
    )

    candles["ema9"] = ema(
        candles["close"],
        9
    )

    candles["ema21"] = ema(
        candles["close"],
        21
    )

    # Find signal candle
    eligible = candles[
        candles["timestamp"] <= signal_ts
    ]

    if eligible.empty:

        result["ema3_status"] = "NO_SIGNAL_CANDLE"
        results.append(result)
        continue

    signal_idx = eligible.index[-1]

    sig = candles.loc[signal_idx]

    result["ema3_at_signal"] = sig["ema3"]
    result["ema9_at_signal"] = sig["ema9"]
    result["ema21_at_signal"] = sig["ema21"]

    aligned_now = alignment(
        side,
        sig["ema3"],
        sig["ema9"],
        sig["ema21"]
    )

    result[
        "ema3_aligned_at_signal"
    ] = aligned_now

    if aligned_now:

        result[
            "ema3_eventually_aligned"
        ] = True

        result["alignment_ts"] = sig[
            "timestamp"
        ]

        result["wait_candles"] = 0

        result["ema3_status"] = (
            "PASS_IMMEDIATE"
        )

    else:

        # Wait maximum N candles
        future = candles.loc[
            signal_idx + 1:
            signal_idx + MAX_WAIT_CANDLES
        ]

        found = False

        for wait, (_, candle) in enumerate(
            future.iterrows(),
            start=1
        ):

            ok = alignment(
                side,
                candle["ema3"],
                candle["ema9"],
                candle["ema21"]
            )

            if ok:

                result[
                    "ema3_eventually_aligned"
                ] = True

                result[
                    "alignment_ts"
                ] = candle["timestamp"]

                result[
                    "wait_candles"
                ] = wait

                result[
                    "ema3_status"
                ] = "PASS_DELAYED"

                found = True
                break

        if not found:

            result[
                "ema3_status"
            ] = "BLOCK_EMA3"

    results.append(result)

    print(
        f"{date} {symbol:<12} "
        f"{side:<4} "
        f"{result['ema3_status']:<15} "
        f"net={trade['net']:+.2f}"
    )


# ============================================================
# RESULTS
# ============================================================

r = pd.DataFrame(results)

r["ema3_pass"] = r[
    "ema3_status"
].isin([
    "PASS_IMMEDIATE",
    "PASS_DELAYED"
])

known = r[
    ~r["ema3_status"].isin([
        "NO_CANDLES",
        "NO_SIDE",
        "NO_SIGNAL_CANDLE"
    ])
].copy()

survived = known[
    known["ema3_pass"]
].copy()

blocked = known[
    ~known["ema3_pass"]
].copy()


# ============================================================
# BASELINE
# ============================================================

print()
print("=" * 120)
print("ORIGINAL 3M BASELINE")
print("=" * 120)

print(
    f"Trades : {len(trades)}"
)

print(
    f"Wins   : {(trades['net'] > 0).sum()}"
)

print(
    f"Losses : {(trades['net'] < 0).sum()}"
)

print(
    f"Gross  : Rs {trades['gross'].sum():.2f}"
)

print(
    f"Costs  : Rs {trades['costs'].sum():.2f}"
)

print(
    f"NET    : Rs {trades['net'].sum():.2f}"
)


# ============================================================
# COVERAGE
# ============================================================

print()
print("=" * 120)
print("EMA3 DATA COVERAGE")
print("=" * 120)

print(
    r["ema3_status"]
    .value_counts(dropna=False)
    .to_string()
)


# ============================================================
# SURVIVAL
# ============================================================

print()
print("=" * 120)
print("EMA3 SURVIVAL — ORIGINAL TRADE PNL")
print("=" * 120)

print(
    f"Comparable trades : {len(known)}"
)

print(
    f"Survived          : {len(survived)}"
)

print(
    f"Blocked           : {len(blocked)}"
)

if len(known):

    print(
        f"Survival rate     : "
        f"{100*len(survived)/len(known):.1f}%"
    )

print()

if len(survived):

    print(
        f"Survived winners : "
        f"{(survived['net'] > 0).sum()}"
    )

    print(
        f"Survived losers  : "
        f"{(survived['net'] < 0).sum()}"
    )

    print(
        f"Gross            : "
        f"Rs {survived['gross'].sum():.2f}"
    )

    print(
        f"Costs            : "
        f"Rs {survived['costs'].sum():.2f}"
    )

    print(
        f"NET              : "
        f"Rs {survived['net'].sum():.2f}"
    )


# ============================================================
# WINNER RETENTION
# ============================================================

print()
print("=" * 120)
print("WINNER RETENTION")
print("=" * 120)

known_winners = known[
    known["net"] > 0
]

retained_winners = known_winners[
    known_winners["ema3_pass"]
]

blocked_winners = known_winners[
    ~known_winners["ema3_pass"]
]

print(
    f"Known winners    : {len(known_winners)}"
)

print(
    f"Retained winners : {len(retained_winners)}"
)

print(
    f"Blocked winners  : {len(blocked_winners)}"
)

print(
    f"Blocked winner P&L : "
    f"Rs {blocked_winners['net'].sum():.2f}"
)


# ============================================================
# LOSER REMOVAL
# ============================================================

print()
print("=" * 120)
print("LOSER REMOVAL")
print("=" * 120)

known_losers = known[
    known["net"] < 0
]

retained_losers = known_losers[
    known_losers["ema3_pass"]
]

blocked_losers = known_losers[
    ~known_losers["ema3_pass"]
]

print(
    f"Known losers    : {len(known_losers)}"
)

print(
    f"Retained losers : {len(retained_losers)}"
)

print(
    f"Blocked losers  : {len(blocked_losers)}"
)

print(
    f"Blocked loser P&L : "
    f"Rs {blocked_losers['net'].sum():.2f}"
)


# ============================================================
# TRADE DETAIL
# ============================================================

print()
print("=" * 120)
print("TRADE-BY-TRADE")
print("=" * 120)

cols = [
    "date",
    "signal_ts",
    "symbol",
    "decision",
    "actual_side",
    "net",
    "ema3_at_signal",
    "ema9_at_signal",
    "ema21_at_signal",
    "ema3_status",
    "alignment_ts",
    "wait_candles",
]

print(
    r[cols].to_string(index=False)
)


# ============================================================
# IMPORTANT WARNING
# ============================================================

print()
print("=" * 120)
print("IMPORTANT")
print("=" * 120)

print(
    "The P&L above uses the ORIGINAL trade P&L for trades "
    "that survive EMA3."
)

print(
    "PASS_DELAYED trades entered later, therefore their true "
    "entry price and P&L must be re-simulated separately."
)

print(
    "Do NOT call the survived P&L the final EMA3 strategy P&L "
    "until delayed entries are price-replayed."
)


# ============================================================
# SAVE
# ============================================================

r.to_csv(
    OUT / "trade_level.csv",
    index=False
)

survived.to_csv(
    OUT / "survived.csv",
    index=False
)

blocked.to_csv(
    OUT / "blocked.csv",
    index=False
)

print()
print(
    "Saved:",
    OUT
)
