from pathlib import Path
import pandas as pd
import numpy as np

# ============================================================
# INPUTS
# ============================================================

CANDIDATES = [
    Path(
        "runtime/watchlist_missed_opportunity/"
        "market_sector_timeframe_comparison/feature_comparison.csv"
    ),
    Path(
        "runtime/watchlist_missed_opportunity/"
        "direction_regime_test/base_normal_reverse.csv"
    ),
    Path(
        "runtime/watchlist_missed_opportunity/"
        "market_sector_stock_direction_test/feature_level.csv"
    ),
]

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "reverse_only_strategy_test"
)

OUT.mkdir(parents=True, exist_ok=True)

EXCLUDED_DATE = "2026-07-30"


# ============================================================
# FIND BEST SOURCE
# ============================================================

source = None
df = None

for p in CANDIDATES:
    if not p.exists():
        continue

    try:
        x = pd.read_csv(p)
    except Exception:
        continue

    cols = set(x.columns)

    # Need enough information to compare normal/reverse.
    if (
        "normal_net" in cols
        and "reverse_net" in cols
    ):
        source = p
        df = x
        break

if df is None:
    raise SystemExit(
        "Could not find a historical file containing "
        "normal_net and reverse_net."
    )

print()
print("=" * 120)
print("SOURCE")
print("=" * 120)
print(source)
print("Rows:", len(df))
print("Columns:", list(df.columns))


# ============================================================
# NORMALIZE
# ============================================================

if "date" in df.columns:
    df["date"] = (
        pd.to_datetime(
            df["date"],
            errors="coerce"
        )
        .dt.date
        .astype(str)
    )

if "symbol" in df.columns:
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

if "date" in df.columns:
    df = df[
        df["date"] != EXCLUDED_DATE
    ].copy()


# ============================================================
# DETERMINE CURRENT DECISION
# ============================================================

def current_net(row):

    decision = str(
        row.get("decision", "")
    ).upper().strip()

    if decision == "NORMAL":
        return row["normal_net"]

    if decision == "REVERSE":
        return row["reverse_net"]

    # Try old/current decision fields
    for c in [
        "old_decision",
        "final_decision",
        "selected_decision"
    ]:
        if c in row.index:
            x = str(row[c]).upper().strip()

            if x == "NORMAL":
                return row["normal_net"]

            if x == "REVERSE":
                return row["reverse_net"]

    return np.nan


df["current_net"] = df.apply(
    current_net,
    axis=1
)


# ============================================================
# STRATEGIES
# ============================================================

# 1. Always use the original/current direction if available.
current = df.dropna(
    subset=["current_net"]
).copy()

# 2. Always reverse every candidate.
reverse_all = df.copy()
reverse_all["strategy_net"] = (
    reverse_all["reverse_net"]
)

# 3. Reverse only BUY-origin signals.
# We saw this benchmark perform strongly before.
reverse_buy_only = df.copy()

if "direction" in df.columns:
    reverse_buy_only["strategy_net"] = np.where(
        reverse_buy_only["direction"]
        .astype(str)
        .str.upper()
        .eq("BUY"),
        reverse_buy_only["reverse_net"],
        reverse_buy_only["normal_net"]
    )
else:
    reverse_buy_only["strategy_net"] = np.nan


# 4. TAKE ONLY trades where historical/current decision = REVERSE.
reverse_only = df.copy()

decision_col = None

for c in [
    "decision",
    "old_decision",
    "final_decision",
    "selected_decision"
]:
    if c in df.columns:
        decision_col = c
        break

if decision_col is not None:

    reverse_only = reverse_only[
        reverse_only[decision_col]
        .astype(str)
        .str.upper()
        .eq("REVERSE")
    ].copy()

    reverse_only["strategy_net"] = (
        reverse_only["reverse_net"]
    )

else:
    reverse_only = pd.DataFrame()


# ============================================================
# SUMMARY FUNCTION
# ============================================================

def summarize(name, x, pnl_col):

    if x.empty:
        return {
            "strategy": name,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0,
            "net": 0,
            "avg_net": 0,
            "avg_winner": 0,
            "avg_loser": 0,
            "profit_factor": np.nan,
            "max_drawdown": 0,
            "profitable_days": 0,
            "losing_days": 0,
        }

    pnl = pd.to_numeric(
        x[pnl_col],
        errors="coerce"
    ).dropna()

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    cumulative = pnl.cumsum()

    drawdown = (
        cumulative
        - cumulative.cummax()
    )

    pf = (
        wins.sum()
        / abs(losses.sum())
        if len(losses)
        and abs(losses.sum()) > 0
        else np.inf
    )

    profitable_days = np.nan
    losing_days = np.nan

    if "date" in x.columns:

        day = (
            x.assign(
                _pnl=pd.to_numeric(
                    x[pnl_col],
                    errors="coerce"
                )
            )
            .groupby("date")["_pnl"]
            .sum()
        )

        profitable_days = int(
            (day > 0).sum()
        )

        losing_days = int(
            (day < 0).sum()
        )

    return {
        "strategy": name,
        "trades": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(pnl) * 100
            if len(pnl)
            else 0,
        "net": pnl.sum(),
        "avg_net": pnl.mean()
            if len(pnl)
            else 0,
        "avg_winner": wins.mean()
            if len(wins)
            else 0,
        "avg_loser": losses.mean()
            if len(losses)
            else 0,
        "profit_factor": pf,
        "max_drawdown": drawdown.min()
            if len(drawdown)
            else 0,
        "profitable_days": profitable_days,
        "losing_days": losing_days,
    }


rows = []

rows.append(
    summarize(
        "CURRENT_DECISIONS",
        current,
        "current_net"
    )
)

rows.append(
    summarize(
        "ALL_REVERSE",
        reverse_all,
        "strategy_net"
    )
)

if "direction" in df.columns:
    rows.append(
        summarize(
            "REVERSE_BUY_ONLY",
            reverse_buy_only.dropna(
                subset=["strategy_net"]
            ),
            "strategy_net"
        )
    )

if not reverse_only.empty:
    rows.append(
        summarize(
            "REVERSE_ONLY",
            reverse_only,
            "strategy_net"
        )
    )

summary = pd.DataFrame(rows)


# ============================================================
# OUTPUT
# ============================================================

print()
print("=" * 120)
print("STRATEGY COMPARISON")
print("=" * 120)

fmt = summary.copy()

for c in [
    "win_rate_pct",
    "net",
    "avg_net",
    "avg_winner",
    "avg_loser",
    "profit_factor",
    "max_drawdown"
]:
    if c in fmt.columns:
        fmt[c] = fmt[c].map(
            lambda x:
            f"{x:.2f}"
            if pd.notna(x)
            else "nan"
        )

print(
    fmt.to_string(
        index=False
    )
)


# ============================================================
# REVERSE ONLY DAYWISE
# ============================================================

if not reverse_only.empty:

    print()
    print("=" * 120)
    print("REVERSE ONLY — DAYWISE")
    print("=" * 120)

    day = (
        reverse_only
        .assign(
            pnl=reverse_only[
                "strategy_net"
            ]
        )
        .groupby("date")
        .agg(
            trades=("pnl", "size"),
            wins=(
                "pnl",
                lambda x:
                    int((x > 0).sum())
            ),
            losses=(
                "pnl",
                lambda x:
                    int((x < 0).sum())
            ),
            net=("pnl", "sum")
        )
        .reset_index()
    )

    day["win_rate_pct"] = (
        day["wins"]
        / day["trades"]
        * 100
    )

    day["cumulative_net"] = (
        day["net"].cumsum()
    )

    print(
        day.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.2f}"
        )
    )

    day.to_csv(
        OUT / "reverse_only_daywise.csv",
        index=False
    )


# ============================================================
# REVERSE ONLY BY ORIGINAL DIRECTION
# ============================================================

if (
    not reverse_only.empty
    and "direction"
    in reverse_only.columns
):

    print()
    print("=" * 120)
    print("REVERSE ONLY — ORIGINAL BUY VS SELL")
    print("=" * 120)

    breakdown = (
        reverse_only
        .assign(
            original_direction=
                reverse_only[
                    "direction"
                ]
                .astype(str)
                .str.upper(),
            pnl=reverse_only[
                "strategy_net"
            ]
        )
        .groupby(
            "original_direction"
        )
        .agg(
            trades=("pnl", "size"),
            wins=(
                "pnl",
                lambda x:
                    int((x > 0).sum())
            ),
            losses=(
                "pnl",
                lambda x:
                    int((x < 0).sum())
            ),
            net=("pnl", "sum"),
            avg_net=("pnl", "mean")
        )
        .reset_index()
    )

    breakdown["win_rate_pct"] = (
        breakdown["wins"]
        / breakdown["trades"]
        * 100
    )

    print(
        breakdown.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.2f}"
        )
    )

    breakdown.to_csv(
        OUT /
        "reverse_only_by_original_direction.csv",
        index=False
    )


# ============================================================
# SAVE
# ============================================================

summary.to_csv(
    OUT / "summary.csv",
    index=False
)

if not reverse_only.empty:
    reverse_only.to_csv(
        OUT / "reverse_only_trades.csv",
        index=False
    )

print()
print("=" * 120)
print("IMPORTANT")
print("=" * 120)

print(
    "This test compares historical NORMAL/REVERSE outcomes."
)

print(
    "If REVERSE_ONLY wins, do not immediately change live."
)

print(
    "We should next walk-forward test it by date so that "
    "future outcomes are not used to choose the rule."
)

print()
print("Saved:", OUT)

