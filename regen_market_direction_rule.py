from pathlib import Path
import pandas as pd
import numpy as np

SRC = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/feature_comparison.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_direction_forced_bullish_buy_normal"
)

OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(SRC)

df["date"] = pd.to_datetime(
    df["date"], errors="coerce"
).dt.date.astype(str)

df["direction"] = (
    df["direction"]
    .astype(str)
    .str.upper()
    .str.strip()
)

df["market_trend"] = (
    df["market_trend"]
    .astype(str)
    .str.upper()
    .str.strip()
)

for c in [
    "normal_net",
    "reverse_net",
    "normal_gross",
    "reverse_gross",
    "normal_costs",
    "reverse_costs",
]:
    if c in df.columns:
        df[c] = pd.to_numeric(
            df[c], errors="coerce"
        ).fillna(0)

# Exclude old problematic date
df = df[
    df["date"] != "2026-07-30"
].copy()


# ============================================================
# FIXED PROPOSED RULE
# ============================================================

def choose(row):

    market = row["market_trend"]
    direction = row["direction"]

    # BEARISH
    if market == "BEARISH":

        if direction == "BUY":
            return "NORMAL"

        if direction == "SELL":
            return "REVERSE"

    # BULLISH
    if market == "BULLISH":

        # USER REQUEST:
        # bullish + buy MUST remain normal
        if direction == "BUY":
            return "NORMAL"

        if direction == "SELL":
            return "NORMAL"

    # SIDEWAYS
    if market == "SIDEWAYS":

        if direction == "BUY":
            return "REVERSE"

        if direction == "SELL":
            return "NORMAL"

    return "SKIP"


df["new_decision"] = df.apply(
    choose,
    axis=1
)


# ============================================================
# ACTUAL EXECUTED SIDE
# ============================================================

def actual_side(row):

    decision = row["new_decision"]
    direction = row["direction"]

    if decision == "NORMAL":
        return direction

    if decision == "REVERSE":
        return (
            "SELL"
            if direction == "BUY"
            else "BUY"
        )

    return "SKIP"


df["actual_side"] = df.apply(
    actual_side,
    axis=1
)


# ============================================================
# PNL
# ============================================================

def pnl(row):

    if row["new_decision"] == "NORMAL":
        return row["normal_net"]

    if row["new_decision"] == "REVERSE":
        return row["reverse_net"]

    return 0.0


def gross(row):

    if row["new_decision"] == "NORMAL":
        return row.get("normal_gross", 0)

    if row["new_decision"] == "REVERSE":
        return row.get("reverse_gross", 0)

    return 0.0


def costs(row):

    if row["new_decision"] == "NORMAL":
        return row.get("normal_costs", 0)

    if row["new_decision"] == "REVERSE":
        return row.get("reverse_costs", 0)

    return 0.0


df["strategy_net"] = df.apply(
    pnl, axis=1
)

df["strategy_gross"] = df.apply(
    gross, axis=1
)

df["strategy_costs"] = df.apply(
    costs, axis=1
)


# Only executed trades
taken = df[
    df["new_decision"] != "SKIP"
].copy()

pnl_series = taken["strategy_net"]

wins = pnl_series[
    pnl_series > 0
]

losses = pnl_series[
    pnl_series < 0
]

curve = pnl_series.cumsum()

drawdown = (
    curve - curve.cummax()
)

profit_factor = (
    wins.sum() / abs(losses.sum())
    if len(losses) and abs(losses.sum()) > 0
    else np.inf
)


# ============================================================
# SUMMARY
# ============================================================

print()
print("=" * 120)
print("FORCED MARKET + DIRECTION RULE")
print("=" * 120)

print("""
BEARISH  + BUY  -> NORMAL
BEARISH  + SELL -> REVERSE

BULLISH  + BUY  -> NORMAL   <-- FORCED
BULLISH  + SELL -> NORMAL

SIDEWAYS + BUY  -> REVERSE
SIDEWAYS + SELL -> NORMAL
""")

print("=" * 120)
print("OVERALL RESULT")
print("=" * 120)

print(f"Rows          : {len(df)}")
print(f"Trades        : {len(taken)}")
print(f"Wins          : {len(wins)}")
print(f"Losses        : {len(losses)}")

print(
    f"Win rate      : "
    f"{len(wins)/len(taken)*100:.2f}%"
)

print(
    f"Gross         : "
    f"Rs {taken['strategy_gross'].sum():.2f}"
)

print(
    f"Costs         : "
    f"Rs {taken['strategy_costs'].sum():.2f}"
)

print(
    f"NET           : "
    f"Rs {taken['strategy_net'].sum():.2f}"
)

print(
    f"Average/trade : "
    f"Rs {taken['strategy_net'].mean():.2f}"
)

print(
    f"Profit factor : "
    f"{profit_factor:.2f}"
)

print(
    f"Max drawdown  : "
    f"Rs {drawdown.min():.2f}"
)


# ============================================================
# MARKET + ORIGINAL DIRECTION BREAKDOWN
# ============================================================

rows = []

for (
    market,
    direction
), g in taken.groupby(
    [
        "market_trend",
        "direction"
    ]
):

    p = g["strategy_net"]

    rows.append({
        "market": market,
        "signal": direction,
        "decision":
            g["new_decision"].iloc[0],
        "actual_side":
            g["actual_side"].iloc[0],
        "trades": len(g),
        "wins": int((p > 0).sum()),
        "losses": int((p < 0).sum()),
        "win_rate_pct":
            (p > 0).mean() * 100,
        "net": p.sum(),
        "avg_net": p.mean(),
    })

breakdown = pd.DataFrame(rows)

print()
print("=" * 120)
print("MARKET + SIGNAL BREAKDOWN")
print("=" * 120)

print(
    breakdown.to_string(
        index=False,
        float_format=lambda x: f"{x:.2f}"
    )
)


# ============================================================
# DAYWISE
# ============================================================

day = (
    taken.groupby("date")
    .agg(
        trades=("strategy_net", "size"),
        wins=(
            "strategy_net",
            lambda x: int((x > 0).sum())
        ),
        losses=(
            "strategy_net",
            lambda x: int((x < 0).sum())
        ),
        net=("strategy_net", "sum")
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

print()
print("=" * 120)
print("DAYWISE")
print("=" * 120)

print(
    day.to_string(
        index=False,
        float_format=lambda x: f"{x:.2f}"
    )
)


# ============================================================
# DECISION COUNTS
# ============================================================

print()
print("=" * 120)
print("DECISIONS")
print("=" * 120)

print(
    taken["new_decision"]
    .value_counts()
    .to_string()
)

print()
print("=" * 120)
print("ACTUAL BUY / SELL")
print("=" * 120)

print(
    taken["actual_side"]
    .value_counts()
    .to_string()
)


# ============================================================
# SAVE
# ============================================================

taken.to_csv(
    OUT / "trade_level.csv",
    index=False
)

breakdown.to_csv(
    OUT / "breakdown.csv",
    index=False
)

day.to_csv(
    OUT / "daywise.csv",
    index=False
)

print()
print("Saved:", OUT)

