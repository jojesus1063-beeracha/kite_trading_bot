from pathlib import Path
import pandas as pd
import numpy as np

# ============================================================
# PATHS
# ============================================================

TRADES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/trades_3m.csv"
)

FEATURES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/feature_comparison.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/candles_3minute"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "ema3_normal_reverse_variants"
)
OUT.mkdir(parents=True, exist_ok=True)

# Maximum candles allowed for delayed confirmation.
MAX_WAIT = 3


# ============================================================
# HELPERS
# ============================================================

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def normalize_ts(s):
    s = pd.to_datetime(s, errors="coerce")

    try:
        if s.dt.tz is None:
            s = s.dt.tz_localize("Asia/Kolkata")
        else:
            s = s.dt.tz_convert("Asia/Kolkata")
    except Exception:
        pass

    return s


def load_candles(symbol):
    path = CANDLE_DIR / f"{symbol}.parquet"

    if not path.exists():
        return None

    try:
        df = pd.read_parquet(path)
    except Exception:
        return None

    # Find timestamp column
    ts_col = None
    for c in ["timestamp", "date", "datetime", "time"]:
        if c in df.columns:
            ts_col = c
            break

    if ts_col is None:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            ts_col = df.columns[0]
        else:
            return None

    df[ts_col] = normalize_ts(df[ts_col])

    if "close" not in df.columns:
        return None

    df = df.dropna(subset=[ts_col, "close"]).copy()
    df = df.sort_values(ts_col).drop_duplicates(ts_col)

    df["ema3"] = ema(df["close"], 3)
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)

    return df.rename(columns={ts_col: "timestamp"})


def aligned(row, side):
    if side == "BUY":
        return (
            row["ema3"] > row["ema9"] >
            row["ema21"]
        )

    return (
        row["ema3"] < row["ema9"] <
        row["ema21"]
    )


def opposite_alignment(row, side):
    """
    For a reversal setup we expect the EMA stack to initially
    point AGAINST the intended trade.

    SELL reversal:
        EMA3 > EMA9 > EMA21

    BUY reversal:
        EMA3 < EMA9 < EMA21
    """

    if side == "SELL":
        return (
            row["ema3"] > row["ema9"] >
            row["ema21"]
        )

    return (
        row["ema3"] < row["ema9"] <
        row["ema21"]
    )


def ema3_turn(prev, cur, side):
    """
    Earliest reversal timing signal.

    SELL:
        EMA3 stops rising / turns downward.

    BUY:
        EMA3 stops falling / turns upward.

    This deliberately does NOT require full
    EMA3/EMA9/EMA21 alignment.
    """

    if side == "SELL":
        return cur["ema3"] < prev["ema3"]

    return cur["ema3"] > prev["ema3"]


def ema3_cross9(prev, cur, side):
    """
    Stronger reversal confirmation.

    SELL:
        EMA3 crosses below EMA9.

    BUY:
        EMA3 crosses above EMA9.
    """

    if side == "SELL":
        return (
            prev["ema3"] >= prev["ema9"] and
            cur["ema3"] < cur["ema9"]
        )

    return (
        prev["ema3"] <= prev["ema9"] and
        cur["ema3"] > cur["ema9"]
    )


# ============================================================
# LOAD TRADE DATA
# ============================================================

trades = pd.read_csv(TRADES)

trades["date"] = (
    pd.to_datetime(trades["date"], errors="coerce")
    .dt.date.astype(str)
)

trades["signal_ts"] = normalize_ts(trades["signal_ts"])

trades["symbol"] = (
    trades["symbol"]
    .astype(str)
    .str.upper()
    .str.strip()
)

for c in ["gross", "costs", "net"]:
    trades[c] = pd.to_numeric(
        trades[c], errors="coerce"
    ).fillna(0)


# ============================================================
# GET EXECUTED SIDE
# ============================================================

features = None

if FEATURES.exists():
    features = pd.read_csv(FEATURES)

    if "symbol" in features.columns:
        features["symbol"] = (
            features["symbol"]
            .astype(str)
            .str.upper()
            .str.strip()
        )

    for c in ["signal_ts", "timestamp"]:
        if c in features.columns:
            features[c] = normalize_ts(features[c])


def infer_side(trade):
    """
    First try feature_comparison.
    Then use any side/direction columns present in trade data.
    """

    # Direct side in trades
    for c in [
        "executed_side",
        "actual_side",
        "side",
        "direction"
    ]:
        if c in trade.index:
            x = str(trade[c]).upper().strip()

            if x in ["BUY", "SELL"]:
                return x

    # Feature file
    if features is not None:
        f = features[
            features["symbol"] == trade["symbol"]
        ].copy()

        if len(f):
            ts_col = None

            if "signal_ts" in f.columns:
                ts_col = "signal_ts"
            elif "timestamp" in f.columns:
                ts_col = "timestamp"

            if ts_col:
                delta = (
                    f[ts_col] - trade["signal_ts"]
                ).abs()

                if delta.notna().any():
                    x = f.loc[delta.idxmin()]

                    # Look for actual/executed direction first
                    for c in [
                        "executed_side",
                        "actual_side",
                        "side",
                        "direction"
                    ]:
                        if c in x.index:
                            v = str(x[c]).upper().strip()

                            if v in ["BUY", "SELL"]:
                                return v

    return None


# ============================================================
# TEST EACH TRADE
# ============================================================

rows = []

for _, trade in trades.iterrows():

    symbol = trade["symbol"]
    date = trade["date"]
    signal_ts = trade["signal_ts"]
    decision = str(
        trade.get("decision", "")
    ).upper().strip()

    side = infer_side(trade)

    base = {
        "date": date,
        "signal_ts": signal_ts,
        "symbol": symbol,
        "decision": decision,
        "side": side,
        "gross": trade["gross"],
        "costs": trade["costs"],
        "net": trade["net"],
    }

    candles = load_candles(symbol)

    if candles is None or side not in ["BUY", "SELL"]:
        rows.append({
            **base,
            "data_status": "NO_DATA",
        })
        continue

    day = candles[
        candles["timestamp"].dt.date.astype(str)
        == date
    ].copy()

    if day.empty:
        rows.append({
            **base,
            "data_status": "NO_DATE_CANDLES",
        })
        continue

    # Candle at or immediately before signal
    before = day[
        day["timestamp"] <= signal_ts
    ]

    if before.empty:
        rows.append({
            **base,
            "data_status": "NO_SIGNAL_CANDLE",
        })
        continue

    idx = before.index[-1]
    pos = day.index.get_loc(idx)

    current = day.iloc[pos]

    # Future candles for delayed checks
    future = day.iloc[
        pos:min(pos + MAX_WAIT + 1, len(day))
    ].reset_index(drop=True)

    if len(future) == 0:
        rows.append({
            **base,
            "data_status": "NO_FUTURE",
        })
        continue

    # --------------------------------------------------------
    # VARIANT 1
    # CURRENT STRATEGY
    # --------------------------------------------------------

    v1 = True

    # --------------------------------------------------------
    # VARIANT 2
    # HARD EMA3 ON EVERYTHING
    # --------------------------------------------------------

    v2 = aligned(current, side)

    # --------------------------------------------------------
    # VARIANT 3
    # EMA3 HARD GATE ONLY FOR NORMAL
    #
    # NORMAL:
    #   must have full EMA alignment
    #
    # REVERSE:
    #   unchanged
    # --------------------------------------------------------

    if decision == "NORMAL":
        v3 = aligned(current, side)
    else:
        v3 = True

    # --------------------------------------------------------
    # VARIANT 4
    # NORMAL:
    #   full EMA alignment
    #
    # REVERSE:
    #   allow setup if EMA stack is against trade,
    #   then wait for EMA3 to TURN toward trade.
    # --------------------------------------------------------

    v4 = False
    v4_wait = np.nan
    v4_reason = None

    if decision == "NORMAL":

        if aligned(current, side):
            v4 = True
            v4_wait = 0
            v4_reason = "NORMAL_FULL_ALIGNMENT"
        else:
            v4_reason = "NORMAL_NOT_ALIGNED"

    else:

        # Reversal should preferably begin while short-term
        # EMA structure is still opposite the intended side.
        initial_opposite = opposite_alignment(
            current, side
        )

        # If already aligned with intended side, reversal has
        # already progressed. We still allow immediate entry.
        if aligned(current, side):
            v4 = True
            v4_wait = 0
            v4_reason = "REVERSE_ALREADY_ALIGNED"

        elif initial_opposite:

            for j in range(1, len(future)):

                prev = future.iloc[j - 1]
                cur = future.iloc[j]

                if ema3_turn(prev, cur, side):
                    v4 = True
                    v4_wait = j
                    v4_reason = "REVERSE_EMA3_TURN"
                    break

            if not v4:
                v4_reason = "REVERSE_NO_TURN"

        else:
            # Mixed EMA state.
            # Wait for either an EMA3 turn or EMA3/EMA9 cross.
            for j in range(1, len(future)):

                prev = future.iloc[j - 1]
                cur = future.iloc[j]

                if (
                    ema3_turn(prev, cur, side)
                    or ema3_cross9(prev, cur, side)
                ):
                    v4 = True
                    v4_wait = j
                    v4_reason = "REVERSE_MIXED_TURN"
                    break

            if not v4:
                v4_reason = "REVERSE_MIXED_NO_CONFIRM"

    rows.append({
        **base,

        "data_status": "OK",

        "ema3": current["ema3"],
        "ema9": current["ema9"],
        "ema21": current["ema21"],

        "v1_current": v1,
        "v2_hard_all": v2,
        "v3_normal_only": v3,
        "v4_reverse_turn": v4,

        "v4_wait_candles": v4_wait,
        "v4_reason": v4_reason,
    })


result = pd.DataFrame(rows)

result.to_csv(
    OUT / "trade_level.csv",
    index=False
)


# ============================================================
# REPORTING
# ============================================================

print()
print("=" * 125)
print("EMA3 NORMAL / REVERSE VARIANT TEST")
print("=" * 125)

print()
print("Baseline")
print("-" * 70)

print(f"Trades : {len(trades)}")
print(f"Wins   : {(trades.net > 0).sum()}")
print(f"Losses : {(trades.net < 0).sum()}")
print(f"NET    : Rs {trades.net.sum():.2f}")


known = result[
    result["data_status"] == "OK"
].copy()

print()
print("=" * 125)
print("DATA COVERAGE")
print("=" * 125)

print(result["data_status"].value_counts().to_string())

print()
print(f"Comparable : {len(known)} / {len(result)}")


variants = {
    "V1 CURRENT": "v1_current",
    "V2 EMA3 HARD ALL": "v2_hard_all",
    "V3 EMA3 NORMAL ONLY": "v3_normal_only",
    "V4 NORMAL + REVERSE TURN": "v4_reverse_turn",
}

summary = []

for name, col in variants.items():

    x = known[known[col] == True].copy()
    blocked = known[known[col] != True].copy()

    wins = (x["net"] > 0).sum()
    losses = (x["net"] < 0).sum()

    win_rate = (
        wins / len(x) * 100
        if len(x)
        else 0
    )

    blocked_winners = blocked[
        blocked["net"] > 0
    ]

    blocked_losers = blocked[
        blocked["net"] < 0
    ]

    summary.append({
        "variant": name,
        "trades": len(x),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "original_net_of_survivors": x["net"].sum(),
        "winners_blocked": len(blocked_winners),
        "winner_pnl_blocked": blocked_winners["net"].sum(),
        "losers_blocked": len(blocked_losers),
        "loser_pnl_blocked": blocked_losers["net"].sum(),
    })


summary = pd.DataFrame(summary)

print()
print("=" * 125)
print("VARIANT COMPARISON")
print("=" * 125)

print(
    summary.to_string(
        index=False,
        float_format=lambda x: f"{x:.2f}"
    )
)

summary.to_csv(
    OUT / "variant_summary.csv",
    index=False
)


# ============================================================
# NORMAL VS REVERSE BREAKDOWN
# ============================================================

print()
print("=" * 125)
print("NORMAL VS REVERSE")
print("=" * 125)

for decision in ["NORMAL", "REVERSE"]:

    z = known[
        known["decision"] == decision
    ]

    print()
    print(decision)
    print("-" * 80)

    print(
        f"Trades={len(z)} | "
        f"Wins={(z.net > 0).sum()} | "
        f"Losses={(z.net < 0).sum()} | "
        f"Net=Rs {z.net.sum():.2f}"
    )

    for name, col in variants.items():

        q = z[z[col] == True]

        print(
            f"{name:<28} "
            f"trades={len(q):2d} "
            f"wins={(q.net > 0).sum():2d} "
            f"losses={(q.net < 0).sum():2d} "
            f"net=Rs {q.net.sum():8.2f}"
        )


# ============================================================
# V4 TRADE-BY-TRADE
# ============================================================

print()
print("=" * 125)
print("V4 — NORMAL + REVERSE EMA3 TURN")
print("=" * 125)

cols = [
    "date",
    "signal_ts",
    "symbol",
    "decision",
    "side",
    "net",
    "ema3",
    "ema9",
    "ema21",
    "v4_reverse_turn",
    "v4_wait_candles",
    "v4_reason",
]

print(
    known[cols].to_string(
        index=False,
        float_format=lambda x: f"{x:.4f}"
    )
)


# ============================================================
# V4 WINNERS BLOCKED
# ============================================================

blocked = known[
    known["v4_reverse_turn"] != True
]

bw = blocked[blocked["net"] > 0]
bl = blocked[blocked["net"] < 0]

print()
print("=" * 125)
print("V4 BLOCKED WINNERS")
print("=" * 125)

if len(bw):
    print(
        bw[
            [
                "date",
                "symbol",
                "decision",
                "side",
                "net",
                "v4_reason",
            ]
        ].to_string(index=False)
    )
else:
    print("None")


print()
print("=" * 125)
print("V4 BLOCKED LOSERS")
print("=" * 125)

if len(bl):
    print(
        bl[
            [
                "date",
                "symbol",
                "decision",
                "side",
                "net",
                "v4_reason",
            ]
        ].to_string(index=False)
    )
else:
    print("None")


# ============================================================
# IMPORTANT WARNING
# ============================================================

print()
print("=" * 125)
print("IMPORTANT")
print("=" * 125)

print("""
V1/V2/V3/V4 P&L shown here uses the ORIGINAL P&L of surviving trades.

For V4, a reversal may wait 1-3 candles before confirmation.
Therefore a delayed reversal has a DIFFERENT entry price.

Do not call V4's P&L final until delayed entries are re-priced
and replayed from their actual confirmation candle.

The purpose of this run is first to determine whether EMA3
selectivity improves winner retention / loser removal.
""")

print("Saved:", OUT)

