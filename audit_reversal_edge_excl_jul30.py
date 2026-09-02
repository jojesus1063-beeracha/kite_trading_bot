from pathlib import Path
import math
import pandas as pd
import numpy as np

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test/"
    "base_normal_reverse.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test/"
    "reversal_audit_excl_2026_07_30"
)
OUT.mkdir(parents=True, exist_ok=True)

if not BASE.exists():
    raise SystemExit(f"Missing: {BASE}")

df = pd.read_csv(BASE)

df["signal_ts"] = pd.to_datetime(
    df["signal_ts"],
    errors="coerce"
)

for c in [
    "normal_gross",
    "normal_costs",
    "normal_net",
    "reverse_gross",
    "reverse_costs",
    "reverse_net",
]:
    df[c] = pd.to_numeric(
        df[c],
        errors="coerce"
    )

df = df.dropna(
    subset=[
        "normal_net",
        "reverse_net",
        "signal_ts",
    ]
).copy()

# --------------------------------------------------
# EXCLUDE JULY 30, 2026 FROM THE ENTIRE EXPERIMENT
# --------------------------------------------------

EXCLUDED_DATE = "2026-07-30"

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce"
).dt.strftime("%Y-%m-%d")

rows_before = len(df)

df = df[
    df["date"] != EXCLUDED_DATE
].copy()

rows_removed = rows_before - len(df)

print("===== DATE EXCLUSION =====")
print("Excluded date :", EXCLUDED_DATE)
print("Rows before   :", rows_before)
print("Rows removed  :", rows_removed)
print("Rows remaining:", len(df))

df = df.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

# --------------------------------------------------
# METRICS
# --------------------------------------------------

def drawdown(pnls):
    eq = 0.0
    peak = 0.0
    worst = 0.0

    for p in pnls:
        eq += float(p)
        peak = max(peak, eq)
        worst = min(
            worst,
            eq - peak
        )

    return worst


def metrics(x, prefix):
    if x.empty:
        return {
            "strategy": prefix,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "gross": 0,
            "costs": 0,
            "net": 0,
            "avg_net": 0,
            "avg_winner": 0,
            "avg_loser": 0,
            "profit_factor": np.nan,
            "max_drawdown": 0,
            "largest_win": 0,
            "largest_loss": 0,
        }

    wins = x[x["net"] > 0]
    losses = x[x["net"] <= 0]

    gross_profit = float(
        x.loc[
            x["gross"] > 0,
            "gross"
        ].sum()
    )

    gross_loss = float(
        x.loc[
            x["gross"] < 0,
            "gross"
        ].sum()
    )

    pf = (
        gross_profit / abs(gross_loss)
        if gross_loss < 0
        else np.inf
    )

    return {
        "strategy": prefix,
        "trades": len(x),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(x) * 100,
        "gross_profit":
            gross_profit,
        "gross_loss":
            gross_loss,
        "gross":
            float(x["gross"].sum()),
        "costs":
            float(x["costs"].sum()),
        "net":
            float(x["net"].sum()),
        "avg_net":
            float(x["net"].mean()),
        "avg_winner":
            float(wins["net"].mean())
            if not wins.empty
            else 0.0,
        "avg_loser":
            float(losses["net"].mean())
            if not losses.empty
            else 0.0,
        "profit_factor":
            pf,
        "max_drawdown":
            drawdown(
                x["net"].tolist()
            ),
        "largest_win":
            float(x["net"].max()),
        "largest_loss":
            float(x["net"].min()),
    }


# --------------------------------------------------
# BUILD NORMAL + ALL REVERSE
# --------------------------------------------------

normal = pd.DataFrame({
    "date": df["date"],
    "signal_ts": df["signal_ts"],
    "symbol": df["symbol"],
    "original_direction":
        df["direction"],
    "executed_direction":
        df["direction"],
    "gross":
        df["normal_gross"],
    "costs":
        df["normal_costs"],
    "net":
        df["normal_net"],
})

all_reverse = pd.DataFrame({
    "date": df["date"],
    "signal_ts": df["signal_ts"],
    "symbol": df["symbol"],
    "original_direction":
        df["direction"],
    "executed_direction":
        df["reverse_direction"],
    "gross":
        df["reverse_gross"],
    "costs":
        df["reverse_costs"],
    "net":
        df["reverse_net"],
})

# --------------------------------------------------
# DAYWISE
# --------------------------------------------------

def daywise(x, name):
    d = (
        x.groupby("date")
        .agg(
            trades=("symbol", "size"),
            wins=("net", lambda s: int((s > 0).sum())),
            losses=("net", lambda s: int((s <= 0).sum())),
            gross=("gross", "sum"),
            costs=("costs", "sum"),
            net=("net", "sum"),
        )
        .reset_index()
    )

    d["strategy"] = name
    d["cumulative_net"] = d["net"].cumsum()

    return d


day_normal = daywise(
    normal,
    "CURRENT_NORMAL"
)

day_reverse = daywise(
    all_reverse,
    "ALL_REVERSE"
)

day_all = pd.concat(
    [
        day_normal,
        day_reverse,
    ],
    ignore_index=True
)

day_all.to_csv(
    OUT / "daywise_normal_vs_reverse.csv",
    index=False
)

# --------------------------------------------------
# OUTLIER TESTS
# --------------------------------------------------

def remove_largest_winner(x):
    if x.empty:
        return x.copy()

    idx = x["net"].idxmax()

    return x.drop(
        index=idx
    ).copy()


def remove_best_day(x):
    if x.empty:
        return x.copy()

    daily = (
        x.groupby("date")["net"]
        .sum()
    )

    best_day = daily.idxmax()

    return x[
        x["date"] != best_day
    ].copy()


reverse_without_best_trade = (
    remove_largest_winner(
        all_reverse
    )
)

reverse_without_best_day = (
    remove_best_day(
        all_reverse
    )
)

# --------------------------------------------------
# CONVERSION MATRIX
# --------------------------------------------------

conv = df.copy()

conv["normal_winner"] = (
    conv["normal_net"] > 0
)

conv["reverse_winner"] = (
    conv["reverse_net"] > 0
)

def conversion_label(r):
    if (
        not r["normal_winner"]
        and r["reverse_winner"]
    ):
        return "NORMAL_LOSER_TO_REVERSE_WINNER"

    if (
        not r["normal_winner"]
        and not r["reverse_winner"]
    ):
        return "NORMAL_LOSER_TO_REVERSE_LOSER"

    if (
        r["normal_winner"]
        and r["reverse_winner"]
    ):
        return "NORMAL_WINNER_TO_REVERSE_WINNER"

    return "NORMAL_WINNER_TO_REVERSE_LOSER"


conv["conversion"] = conv.apply(
    conversion_label,
    axis=1
)

conv_summary = (
    conv.groupby("conversion")
    .agg(
        trades=("symbol", "size"),
        normal_net=("normal_net", "sum"),
        reverse_net=("reverse_net", "sum"),
    )
    .reset_index()
)

conv_summary[
    "delta"
] = (
    conv_summary["reverse_net"]
    -
    conv_summary["normal_net"]
)

conv_summary.to_csv(
    OUT / "conversion_matrix.csv",
    index=False
)

# --------------------------------------------------
# SYMBOL CONCENTRATION
# --------------------------------------------------

symbol_reverse = (
    all_reverse.groupby("symbol")
    .agg(
        trades=("symbol", "size"),
        wins=("net", lambda s: int((s > 0).sum())),
        net=("net", "sum"),
    )
    .reset_index()
    .sort_values(
        "net",
        ascending=False
    )
)

symbol_reverse.to_csv(
    OUT / "reverse_by_symbol.csv",
    index=False
)

# --------------------------------------------------
# SIDE BREAKDOWN
# --------------------------------------------------

side_reverse = (
    all_reverse.groupby(
        [
            "original_direction",
            "executed_direction",
        ]
    )
    .agg(
        trades=("symbol", "size"),
        wins=("net", lambda s: int((s > 0).sum())),
        losses=("net", lambda s: int((s <= 0).sum())),
        net=("net", "sum"),
    )
    .reset_index()
)

side_reverse[
    "win_rate_pct"
] = (
    side_reverse["wins"]
    /
    side_reverse["trades"]
    * 100
)

side_reverse.to_csv(
    OUT / "reverse_by_direction.csv",
    index=False
)

# --------------------------------------------------
# FINAL METRIC TABLE
# --------------------------------------------------

rows = [
    metrics(
        normal,
        "CURRENT_NORMAL"
    ),
    metrics(
        all_reverse,
        "ALL_REVERSE"
    ),
    metrics(
        reverse_without_best_trade,
        "ALL_REVERSE_MINUS_LARGEST_WINNER"
    ),
    metrics(
        reverse_without_best_day,
        "ALL_REVERSE_MINUS_BEST_DAY"
    ),
]

summary = pd.DataFrame(rows)

summary.to_csv(
    OUT / "summary.csv",
    index=False
)

# --------------------------------------------------
# PRINT
# --------------------------------------------------

print(
    "===== NORMAL VS ALL-REVERSE ====="
)

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v: f"{v:.1f}%",
            "gross_profit":
                lambda v: f"Rs {v:,.2f}",
            "gross_loss":
                lambda v: f"Rs {v:,.2f}",
            "gross":
                lambda v: f"Rs {v:,.2f}",
            "costs":
                lambda v: f"Rs {v:,.2f}",
            "net":
                lambda v: f"Rs {v:,.2f}",
            "avg_net":
                lambda v: f"Rs {v:,.2f}",
            "avg_winner":
                lambda v: f"Rs {v:,.2f}",
            "avg_loser":
                lambda v: f"Rs {v:,.2f}",
            "profit_factor":
                lambda v: (
                    "INF"
                    if math.isinf(v)
                    else f"{v:.2f}"
                    if pd.notna(v)
                    else "NA"
                ),
            "max_drawdown":
                lambda v: f"Rs {v:,.2f}",
            "largest_win":
                lambda v: f"Rs {v:,.2f}",
            "largest_loss":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

print(
    "\n===== ALL-REVERSE DAYWISE ====="
)

print(
    day_reverse.to_string(
        index=False,
        formatters={
            "gross":
                lambda v: f"Rs {v:,.2f}",
            "costs":
                lambda v: f"Rs {v:,.2f}",
            "net":
                lambda v: f"Rs {v:,.2f}",
            "cumulative_net":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

print(
    "\n===== CONVERSION MATRIX ====="
)

print(
    conv_summary.to_string(
        index=False,
        formatters={
            "normal_net":
                lambda v: f"Rs {v:,.2f}",
            "reverse_net":
                lambda v: f"Rs {v:,.2f}",
            "delta":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

nlrw = conv[
    conv["conversion"]
    ==
    "NORMAL_LOSER_TO_REVERSE_WINNER"
]

nlrl = conv[
    conv["conversion"]
    ==
    "NORMAL_LOSER_TO_REVERSE_LOSER"
]

nwrw = conv[
    conv["conversion"]
    ==
    "NORMAL_WINNER_TO_REVERSE_WINNER"
]

nwrl = conv[
    conv["conversion"]
    ==
    "NORMAL_WINNER_TO_REVERSE_LOSER"
]

normal_losers = int(
    (~conv["normal_winner"]).sum()
)

normal_winners = int(
    conv["normal_winner"].sum()
)

print(
    "\n===== REVERSAL CONVERSION RATES ====="
)

print(
    "Normal losers:",
    normal_losers
)

print(
    "Normal loser -> reverse winner:",
    len(nlrw),
    f"({len(nlrw)/max(1,normal_losers)*100:.1f}%)"
)

print(
    "Normal loser -> reverse loser:",
    len(nlrl),
    f"({len(nlrl)/max(1,normal_losers)*100:.1f}%)"
)

print()

print(
    "Normal winners:",
    normal_winners
)

print(
    "Normal winner -> reverse winner:",
    len(nwrw),
    f"({len(nwrw)/max(1,normal_winners)*100:.1f}%)"
)

print(
    "Normal winner -> reverse loser:",
    len(nwrl),
    f"({len(nwrl)/max(1,normal_winners)*100:.1f}%)"
)

print(
    "\n===== REVERSE BY DIRECTION ====="
)

print(
    side_reverse.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v: f"{v:.1f}%",
            "net":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

print(
    "\n===== TOP 20 REVERSE SYMBOLS ====="
)

print(
    symbol_reverse.head(20).to_string(
        index=False,
        formatters={
            "net":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

best_reverse_day = (
    day_reverse.sort_values(
        "net",
        ascending=False
    ).iloc[0]
)

worst_reverse_day = (
    day_reverse.sort_values(
        "net"
    ).iloc[0]
)

print(
    "\n===== CONCENTRATION ====="
)

print(
    "Largest reverse winner:",
    f"Rs {all_reverse['net'].max():,.2f}"
)

print(
    "Best reverse day:",
    best_reverse_day["date"],
    f"Rs {best_reverse_day['net']:,.2f}"
)

print(
    "Worst reverse day:",
    worst_reverse_day["date"],
    f"Rs {worst_reverse_day['net']:,.2f}"
)

total_positive = (
    all_reverse.loc[
        all_reverse["net"] > 0,
        "net"
    ].sum()
)

largest = (
    all_reverse["net"].max()
)

best_day_net = (
    best_reverse_day["net"]
)

print(
    "Largest winner % of positive P&L:",
    f"{largest/max(1,total_positive)*100:.1f}%"
)

print(
    "Best day % of positive P&L:",
    f"{best_day_net/max(1,total_positive)*100:.1f}%"
)

print(
    "\nWrote:",
    OUT
)
