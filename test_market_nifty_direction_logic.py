from pathlib import Path
import pandas as pd
import numpy as np

# ============================================================
# INPUT
# ============================================================

SRC = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/feature_comparison.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_nifty_direction_logic"
)

OUT.mkdir(parents=True, exist_ok=True)

EXCLUDED_DATE = "2026-07-30"


# ============================================================
# LOAD
# ============================================================

df = pd.read_csv(SRC)

print()
print("=" * 125)
print("SOURCE")
print("=" * 125)

print("Rows   :", len(df))
print("Columns:", list(df.columns))


# ============================================================
# NORMALIZE
# ============================================================

df["date"] = (
    pd.to_datetime(
        df["date"],
        errors="coerce"
    )
    .dt.date
    .astype(str)
)

df["symbol"] = (
    df["symbol"]
    .astype(str)
    .str.upper()
    .str.strip()
)

for c in [
    "normal_net",
    "reverse_net"
]:
    df[c] = pd.to_numeric(
        df[c],
        errors="coerce"
    )

df = df.dropna(
    subset=[
        "normal_net",
        "reverse_net"
    ]
).copy()

df = df[
    df["date"] != EXCLUDED_DATE
].copy()


# ============================================================
# IDENTIFY MARKET / NIFTY FIELDS
# ============================================================

market_candidates = [
    "market_trend",
    "market_trend_3m",
    "test_market",
    "market_direction",
]

nifty_candidates = [
    "nifty_trend",
    "nifty50_trend",
    "index_trend",
    "nifty_direction",
]

market_col = next(
    (
        c for c in market_candidates
        if c in df.columns
    ),
    None
)

nifty_col = next(
    (
        c for c in nifty_candidates
        if c in df.columns
    ),
    None
)

print()
print("=" * 125)
print("TREND FIELD RESOLUTION")
print("=" * 125)

print("Market column :", market_col)
print("Nifty column  :", nifty_col)


# ============================================================
# IF NIFTY FIELD DOES NOT EXIST, TRY 3M MARKET FIELD
# ============================================================

# Many previous replay files used NIFTY50 as the market trend.
# We must NOT silently duplicate it.
#
# If no separate Nifty field exists, stop and show candidate
# columns containing nifty/index/market so we can resolve it.

if nifty_col is None:

    candidates = [
        c for c in df.columns
        if any(
            k in c.lower()
            for k in [
                "nifty",
                "index",
                "market"
            ]
        )
    ]

    print()
    print("Possible market/index columns:")
    for c in candidates:
        print(" ", c)

    print()
    print(
        "No independent NIFTY trend column found. "
        "We will still analyse MARKET trend alone below."
    )


# ============================================================
# CLEAN TREND LABELS
# ============================================================

def clean_trend(x):

    x = str(x).upper().strip()

    mapping = {
        "UP": "BULLISH",
        "BUY": "BULLISH",
        "BULL": "BULLISH",
        "BULLISH": "BULLISH",

        "DOWN": "BEARISH",
        "SELL": "BEARISH",
        "BEAR": "BEARISH",
        "BEARISH": "BEARISH",

        "FLAT": "SIDEWAYS",
        "NEUTRAL": "SIDEWAYS",
        "SIDEWAYS": "SIDEWAYS",

        "UNKNOWN": "UNKNOWN",
        "NAN": "UNKNOWN",
        "NONE": "UNKNOWN",
        "": "UNKNOWN",
    }

    return mapping.get(
        x,
        x
    )


if market_col:
    df["market_clean"] = (
        df[market_col]
        .apply(clean_trend)
    )
else:
    df["market_clean"] = "UNKNOWN"

if nifty_col:
    df["nifty_clean"] = (
        df[nifty_col]
        .apply(clean_trend)
    )
else:
    df["nifty_clean"] = "UNKNOWN"


# ============================================================
# DIRECTION
# ============================================================

if "direction" in df.columns:

    df["original_direction"] = (
        df["direction"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

else:

    df["original_direction"] = "UNKNOWN"


# ============================================================
# SUMMARY FUNCTION
# ============================================================

def summarize(x, pnl_col):

    pnl = pd.to_numeric(
        x[pnl_col],
        errors="coerce"
    ).dropna()

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    if len(pnl):
        curve = pnl.cumsum()
        dd = (
            curve
            - curve.cummax()
        ).min()
    else:
        dd = 0

    pf = (
        wins.sum()
        / abs(losses.sum())
        if len(losses)
        and abs(losses.sum()) > 0
        else np.inf
    )

    return {
        "trades": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(pnl) * 100
            if len(pnl)
            else 0,
        "net": pnl.sum(),
        "avg_net":
            pnl.mean()
            if len(pnl)
            else 0,
        "profit_factor": pf,
        "max_drawdown": dd,
    }


# ============================================================
# 1. MARKET TREND ALONE
# ============================================================

market_rows = []

for market, g in df.groupby(
    "market_clean",
    dropna=False
):

    n = summarize(
        g,
        "normal_net"
    )

    r = summarize(
        g,
        "reverse_net"
    )

    market_rows.append({
        "market_trend": market,
        "trades": len(g),

        "normal_wins": n["wins"],
        "normal_losses": n["losses"],
        "normal_win_rate_pct":
            n["win_rate_pct"],
        "normal_net": n["net"],
        "normal_avg": n["avg_net"],
        "normal_pf":
            n["profit_factor"],

        "reverse_wins": r["wins"],
        "reverse_losses": r["losses"],
        "reverse_win_rate_pct":
            r["win_rate_pct"],
        "reverse_net": r["net"],
        "reverse_avg": r["avg_net"],
        "reverse_pf":
            r["profit_factor"],

        "reverse_advantage":
            r["net"] - n["net"],

        "better":
            "REVERSE"
            if r["net"] > n["net"]
            else "NORMAL"
    })

market_summary = pd.DataFrame(
    market_rows
)


print()
print("=" * 125)
print("MARKET TREND ONLY — NORMAL VS REVERSE")
print("=" * 125)

print(
    market_summary.to_string(
        index=False,
        float_format=lambda x:
            f"{x:.2f}"
    )
)

market_summary.to_csv(
    OUT / "market_only.csv",
    index=False
)


# ============================================================
# 2. MARKET TREND + ORIGINAL BUY/SELL
# ============================================================

direction_rows = []

for (
    market,
    direction
), g in df.groupby(
    [
        "market_clean",
        "original_direction"
    ],
    dropna=False
):

    n = summarize(
        g,
        "normal_net"
    )

    r = summarize(
        g,
        "reverse_net"
    )

    direction_rows.append({
        "market_trend": market,
        "original_direction": direction,
        "trades": len(g),

        "normal_net": n["net"],
        "normal_win_rate_pct":
            n["win_rate_pct"],

        "reverse_net": r["net"],
        "reverse_win_rate_pct":
            r["win_rate_pct"],

        "reverse_advantage":
            r["net"] - n["net"],

        "better":
            "REVERSE"
            if r["net"] > n["net"]
            else "NORMAL"
    })

market_direction = pd.DataFrame(
    direction_rows
)


print()
print("=" * 125)
print("MARKET TREND + ORIGINAL DIRECTION")
print("=" * 125)

print(
    market_direction.to_string(
        index=False,
        float_format=lambda x:
            f"{x:.2f}"
    )
)

market_direction.to_csv(
    OUT /
    "market_plus_original_direction.csv",
    index=False
)


# ============================================================
# 3. NIFTY TREND ALONE
# ============================================================

if nifty_col:

    rows = []

    for nifty, g in df.groupby(
        "nifty_clean",
        dropna=False
    ):

        n = summarize(
            g,
            "normal_net"
        )

        r = summarize(
            g,
            "reverse_net"
        )

        rows.append({
            "nifty_trend": nifty,
            "trades": len(g),

            "normal_net": n["net"],
            "normal_win_rate_pct":
                n["win_rate_pct"],

            "reverse_net": r["net"],
            "reverse_win_rate_pct":
                r["win_rate_pct"],

            "reverse_advantage":
                r["net"] - n["net"],

            "better":
                "REVERSE"
                if r["net"] > n["net"]
                else "NORMAL"
        })

    nifty_summary = pd.DataFrame(rows)

    print()
    print("=" * 125)
    print("NIFTY TREND ONLY — NORMAL VS REVERSE")
    print("=" * 125)

    print(
        nifty_summary.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.2f}"
        )
    )

    nifty_summary.to_csv(
        OUT / "nifty_only.csv",
        index=False
    )


# ============================================================
# 4. MARKET + NIFTY COMBINATIONS
# ============================================================

if nifty_col:

    combo_rows = []

    for (
        market,
        nifty
    ), g in df.groupby(
        [
            "market_clean",
            "nifty_clean"
        ],
        dropna=False
    ):

        n = summarize(
            g,
            "normal_net"
        )

        r = summarize(
            g,
            "reverse_net"
        )

        combo_rows.append({
            "market_trend": market,
            "nifty_trend": nifty,
            "trades": len(g),

            "normal_net": n["net"],
            "normal_win_rate_pct":
                n["win_rate_pct"],

            "reverse_net": r["net"],
            "reverse_win_rate_pct":
                r["win_rate_pct"],

            "reverse_advantage":
                r["net"] - n["net"],

            "better":
                "REVERSE"
                if r["net"] > n["net"]
                else "NORMAL"
        })

    combo = pd.DataFrame(
        combo_rows
    )

    print()
    print("=" * 125)
    print("MARKET + NIFTY COMBINATIONS")
    print("=" * 125)

    print(
        combo.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.2f}"
        )
    )

    combo.to_csv(
        OUT /
        "market_nifty_combinations.csv",
        index=False
    )


# ============================================================
# 5. MARKET + NIFTY + ORIGINAL DIRECTION
# ============================================================

if nifty_col:

    detailed_rows = []

    for (
        market,
        nifty,
        direction
    ), g in df.groupby(
        [
            "market_clean",
            "nifty_clean",
            "original_direction"
        ],
        dropna=False
    ):

        n = summarize(
            g,
            "normal_net"
        )

        r = summarize(
            g,
            "reverse_net"
        )

        detailed_rows.append({
            "market_trend": market,
            "nifty_trend": nifty,
            "original_direction":
                direction,
            "trades": len(g),

            "normal_net": n["net"],
            "normal_win_rate_pct":
                n["win_rate_pct"],

            "reverse_net": r["net"],
            "reverse_win_rate_pct":
                r["win_rate_pct"],

            "reverse_advantage":
                r["net"] - n["net"],

            "better":
                "REVERSE"
                if r["net"] > n["net"]
                else "NORMAL"
        })

    detailed = pd.DataFrame(
        detailed_rows
    )

    print()
    print("=" * 125)
    print("MARKET + NIFTY + ORIGINAL BUY/SELL")
    print("=" * 125)

    print(
        detailed.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.2f}"
        )
    )

    detailed.to_csv(
        OUT /
        "market_nifty_direction_detail.csv",
        index=False
    )


# ============================================================
# 6. SIMPLE MARKET-DERIVED RULE
# ============================================================

# This is deliberately data-derived:
#
# For each MARKET trend bucket, choose whichever side
# (NORMAL or REVERSE) historically made more money.
#
# This is NOT yet walk-forward safe; it's only diagnostic.

preference = dict(
    zip(
        market_summary[
            "market_trend"
        ],
        market_summary[
            "better"
        ]
    )
)


def choose_market(row):

    pref = preference.get(
        row["market_clean"],
        "NORMAL"
    )

    return (
        row["reverse_net"]
        if pref == "REVERSE"
        else row["normal_net"]
    )


df["market_rule_net"] = df.apply(
    choose_market,
    axis=1
)

market_rule = summarize(
    df,
    "market_rule_net"
)


print()
print("=" * 125)
print("MARKET-TREND-DERIVED RULE")
print("=" * 125)

print(
    "Preference map:",
    preference
)

for k, v in market_rule.items():
    print(
        f"{k:20s}: "
        f"{v:.2f}"
        if isinstance(
            v,
            (float, np.floating)
        )
        else f"{k:20s}: {v}"
    )


# ============================================================
# SAVE
# ============================================================

df.to_csv(
    OUT / "trade_level.csv",
    index=False
)

print()
print("=" * 125)
print("IMPORTANT")
print("=" * 125)

print(
    "The MARKET-TREND-DERIVED rule uses the same historical "
    "data to learn and evaluate the preference."
)

print(
    "It is diagnostic only. If it looks strong, the next "
    "step must be rolling walk-forward by date."
)

if nifty_col is None:
    print(
        "No independent NIFTY trend was found in this file. "
        "Paste the trend-field resolution output and we will "
        "connect the 3-minute NIFTY candle data separately."
    )

print()
print("Saved:", OUT)

