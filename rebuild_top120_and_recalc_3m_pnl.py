from pathlib import Path
import numpy as np
import pandas as pd

# ============================================================
# FILES
# ============================================================

DECISIONS = Path(
    "runtime/watchlist_missed_opportunity/"
    "all_watchlist_decisions.csv"
)

OLD_TOP120 = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_ranked_watchlist/"
    "top120_watchlist.csv"
)

TRADES_3M = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/"
    "trades_3m.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_3m_final_intersection"
)
OUT.mkdir(parents=True, exist_ok=True)

TOP_N = 120

TARGET_DATES = [
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-20",
    "2026-08-21",
]

# ============================================================
# LOAD
# ============================================================

dec = pd.read_csv(DECISIONS)
old_top = pd.read_csv(OLD_TOP120)
trades = pd.read_csv(TRADES_3M)

for df in [dec, old_top, trades]:
    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

for c in [
    "momentum_pct",
    "relative_volume",
    "score",
]:
    if c in dec.columns:
        dec[c] = pd.to_numeric(
            dec[c],
            errors="coerce"
        )

for c in [
    "rank",
    "score",
    "sweet_distance",
    "momentum_pct",
    "relative_volume",
]:
    if c in old_top.columns:
        old_top[c] = pd.to_numeric(
            old_top[c],
            errors="coerce"
        )

for c in [
    "gross",
    "costs",
    "net",
]:
    trades[c] = pd.to_numeric(
        trades[c],
        errors="coerce"
    ).fillna(0.0)

trades["signal_ts"] = pd.to_datetime(
    trades["signal_ts"],
    errors="coerce"
)

# ============================================================
# DISCOVER THE EXISTING TOP120 RANKING STYLE
# ============================================================

print("===== EXISTING TOP120 FILE =====")
print("Rows :", len(old_top))
print("Dates:", sorted(old_top["date"].dropna().unique()))

# The existing replay script used score_stock(momentum, rvol).
# Reproduce the same broad scoring zones used previously.
#
# Important:
# This is ranking logic, NOT old selected/rejected logic.
# ============================================================

def score_stock(momentum, rvol):
    if pd.isna(momentum) or pd.isna(rvol):
        return 0

    score = 0

    # Momentum zone
    if 1.00 <= momentum <= 1.50:
        score += 60
    elif 0.75 <= momentum < 1.00:
        score += 40
    elif 1.50 < momentum <= 2.00:
        score += 40
    elif momentum > 2.00:
        score += 20

    # RVOL zone
    if 1.50 <= rvol <= 2.00:
        score += 40
    elif 2.00 < rvol <= 3.00:
        score += 30
    elif 1.00 <= rvol < 1.50:
        score += 20
    elif rvol > 3.00:
        score += 10

    return score


def sweet_distance(momentum, rvol):
    """
    Smaller is better.

    Sweet centre:
        momentum ~ 1.25
        rvol ~ 1.75
    """
    if pd.isna(momentum) or pd.isna(rvol):
        return np.inf

    return (
        abs(momentum - 1.25)
        +
        abs(rvol - 1.75)
    )


# ============================================================
# REBUILD TOP120 FOR REQUIRED DATES
# ============================================================

rebuilt = []

print("\n===== REBUILD TOP120 =====")

for date in TARGET_DATES:
    day = dec[
        dec["date"] == date
    ].copy()

    if day.empty:
        print(
            f"{date}: NO ROWS in all_watchlist_decisions.csv"
        )
        continue

    # Remove duplicate symbols if any.
    # Keep the row with strongest momentum/RVOL evidence.
    day["computed_score"] = [
        score_stock(m, r)
        for m, r in zip(
            day["momentum_pct"],
            day["relative_volume"]
        )
    ]

    day["computed_sweet_distance"] = [
        sweet_distance(m, r)
        for m, r in zip(
            day["momentum_pct"],
            day["relative_volume"]
        )
    ]

    day = day.sort_values(
        [
            "computed_score",
            "computed_sweet_distance",
            "momentum_pct",
            "relative_volume",
        ],
        ascending=[
            False,
            True,
            False,
            False,
        ]
    )

    day = day.drop_duplicates(
        "symbol",
        keep="first"
    )

    top = day.head(TOP_N).copy()

    top["rank"] = range(
        1,
        len(top) + 1
    )

    top["score"] = top["computed_score"]
    top["sweet_distance"] = (
        top["computed_sweet_distance"]
    )

    rebuilt.append(top)

    print(
        f"{date}: source={len(day):3d} "
        f"top120={len(top):3d} "
        f"score_min={top['score'].min() if len(top) else 'NA'} "
        f"score_max={top['score'].max() if len(top) else 'NA'}"
    )

if rebuilt:
    new_top = pd.concat(
        rebuilt,
        ignore_index=True
    )
else:
    raise SystemExit(
        "Could not rebuild any Top-120 dates."
    )

new_top.to_csv(
    OUT / "rebuilt_top120.csv",
    index=False
)

# ============================================================
# COMPARE REBUILD VS SAVED TOP120 ON AUG 11
# ============================================================

print(
    "\n===== AUG 11 REBUILD VALIDATION ====="
)

saved_11 = old_top[
    old_top["date"] == "2026-08-11"
].copy()

rebuilt_11 = new_top[
    new_top["date"] == "2026-08-11"
].copy()

saved_symbols = set(
    saved_11["symbol"]
)

rebuilt_symbols = set(
    rebuilt_11["symbol"]
)

overlap = (
    saved_symbols
    &
    rebuilt_symbols
)

print(
    "Saved Aug11 Top120 :",
    len(saved_symbols)
)
print(
    "Rebuilt Aug11      :",
    len(rebuilt_symbols)
)
print(
    "Overlap            :",
    len(overlap)
)
print(
    "Overlap %          :",
    round(
        len(overlap)
        /
        max(1, len(saved_symbols))
        * 100,
        1
    ),
)

missing_from_rebuild = sorted(
    saved_symbols - rebuilt_symbols
)

extra_in_rebuild = sorted(
    rebuilt_symbols - saved_symbols
)

print(
    "Saved but not rebuilt:",
    missing_from_rebuild[:20]
)

print(
    "Rebuilt but not saved:",
    extra_in_rebuild[:20]
)

# ============================================================
# INTERSECT WITH 25 3M TRADES
# ============================================================

top_pairs = set(
    zip(
        new_top["date"],
        new_top["symbol"]
    )
)

trades["passes_new_top120"] = [
    (d, s) in top_pairs
    for d, s in zip(
        trades["date"],
        trades["symbol"]
    )
]

trades["new_net"] = np.where(
    trades["passes_new_top120"],
    trades["net"],
    0.0
)

trades["new_gross"] = np.where(
    trades["passes_new_top120"],
    trades["gross"],
    0.0
)

trades["new_costs"] = np.where(
    trades["passes_new_top120"],
    trades["costs"],
    0.0
)

# ============================================================
# ORIGINAL SUMMARY
# ============================================================

print(
    "\n===== ORIGINAL 3M RESULT ====="
)

print(
    "Trades :",
    len(trades)
)
print(
    "Wins   :",
    int((trades["net"] > 0).sum())
)
print(
    "Losses :",
    int((trades["net"] < 0).sum())
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
# NEW SUMMARY
# ============================================================

passed = trades[
    trades["passes_new_top120"]
].copy()

blocked = trades[
    ~trades["passes_new_top120"]
].copy()

print(
    "\n===== NEW TOP120 + 3M RESULT ====="
)

print(
    "Original 3M trades :",
    len(trades)
)
print(
    "Passed new Top120  :",
    len(passed)
)
print(
    "Blocked by Top120  :",
    len(blocked)
)

print(
    "Wins               :",
    int((passed["net"] > 0).sum())
)
print(
    "Losses             :",
    int((passed["net"] < 0).sum())
)

if len(passed):
    print(
        "Win rate           :",
        f"{(passed['net'] > 0).mean()*100:.1f}%"
    )
else:
    print(
        "Win rate           : 0.0%"
    )

print(
    f"Gross              : Rs {passed['gross'].sum():.2f}"
)
print(
    f"Costs              : Rs {passed['costs'].sum():.2f}"
)
print(
    f"NET                : Rs {passed['net'].sum():.2f}"
)

if len(passed):
    print(
        f"Avg net/trade      : Rs {passed['net'].mean():.2f}"
    )

# ============================================================
# WINNER RETENTION
# ============================================================

orig_winners = trades[
    trades["net"] > 0
].copy()

winner_kept = orig_winners[
    orig_winners["passes_new_top120"]
]

winner_blocked = orig_winners[
    ~orig_winners["passes_new_top120"]
]

print(
    "\n===== WINNER RETENTION ====="
)

print(
    "Original winners :",
    len(orig_winners)
)
print(
    "Winners retained :",
    len(winner_kept)
)
print(
    "Winners blocked  :",
    len(winner_blocked)
)

if len(orig_winners):
    print(
        "Retention rate   :",
        f"{len(winner_kept)/len(orig_winners)*100:.1f}%"
    )

print(
    f"Retained winner P&L : Rs {winner_kept['net'].sum():.2f}"
)
print(
    f"Blocked winner P&L  : Rs {winner_blocked['net'].sum():.2f}"
)

print(
    "\n--- WINNERS RETAINED ---"
)

if len(winner_kept):
    print(
        winner_kept[
            [
                "date",
                "signal_ts",
                "symbol",
                "decision",
                "net",
            ]
        ].to_string(index=False)
    )
else:
    print("None")

print(
    "\n--- WINNERS BLOCKED ---"
)

if len(winner_blocked):
    print(
        winner_blocked[
            [
                "date",
                "signal_ts",
                "symbol",
                "decision",
                "net",
            ]
        ].to_string(index=False)
    )
else:
    print("None")

# ============================================================
# LOSER REMOVAL
# ============================================================

orig_losers = trades[
    trades["net"] < 0
].copy()

loser_kept = orig_losers[
    orig_losers["passes_new_top120"]
]

loser_blocked = orig_losers[
    ~orig_losers["passes_new_top120"]
]

print(
    "\n===== LOSER REMOVAL ====="
)

print(
    "Original losers :",
    len(orig_losers)
)
print(
    "Losers retained :",
    len(loser_kept)
)
print(
    "Losers blocked  :",
    len(loser_blocked)
)

if len(orig_losers):
    print(
        "Removal rate    :",
        f"{len(loser_blocked)/len(orig_losers)*100:.1f}%"
    )

print(
    f"Retained loser P&L : Rs {loser_kept['net'].sum():.2f}"
)
print(
    f"Blocked loser P&L  : Rs {loser_blocked['net'].sum():.2f}"
)

print(
    "\n--- LOSERS RETAINED ---"
)

if len(loser_kept):
    print(
        loser_kept[
            [
                "date",
                "signal_ts",
                "symbol",
                "decision",
                "net",
            ]
        ].to_string(index=False)
    )
else:
    print("None")

print(
    "\n--- LOSERS BLOCKED ---"
)

if len(loser_blocked):
    print(
        loser_blocked[
            [
                "date",
                "signal_ts",
                "symbol",
                "decision",
                "net",
            ]
        ].to_string(index=False)
    )
else:
    print("None")

# ============================================================
# DAYWISE NEW PNL
# ============================================================

print(
    "\n===== NEW DAYWISE PNL ====="
)

if len(passed):
    day = (
        passed.groupby("date")
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
                    int((s < 0).sum())
            ),
            gross=("gross", "sum"),
            costs=("costs", "sum"),
            net=("net", "sum"),
        )
        .reset_index()
    )

    day["win_rate_pct"] = (
        day["wins"]
        /
        day["trades"]
        * 100
    )

    day["cumulative_net"] = (
        day["net"].cumsum()
    )

    print(
        day.to_string(
            index=False,
            formatters={
                "win_rate_pct":
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

                "cumulative_net":
                    lambda v:
                        f"Rs {v:.2f}",
            }
        )
    )

    day.to_csv(
        OUT / "daywise.csv",
        index=False
    )
else:
    print("No trades survived.")

# ============================================================
# IMPORTANT STOCK CHECK
# ============================================================

print(
    "\n===== IMPORTANT STOCK CHECK ====="
)

important = [
    "BANDHANBNK",
    "CIEINDIA",
    "CELLO",
    "ZYDUSLIFE",
    "AGARWALEYE",
    "IRCON",
    "ASTERDM",
    "KRONOX",
]

x = trades[
    trades["symbol"].isin(important)
].copy()

print(
    x[
        [
            "date",
            "signal_ts",
            "symbol",
            "decision",
            "net",
            "passes_new_top120",
        ]
    ].to_string(index=False)
)

# ============================================================
# SAVE
# ============================================================

trades.to_csv(
    OUT / "trade_level.csv",
    index=False
)

passed.to_csv(
    OUT / "passed_trades.csv",
    index=False
)

blocked.to_csv(
    OUT / "blocked_trades.csv",
    index=False
)

print(
    "\nWrote:",
    OUT
)
