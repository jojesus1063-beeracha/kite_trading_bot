from pathlib import Path
import pandas as pd
import numpy as np

FILES = [
    Path(
        "runtime/proposed_logic_broker_sl_replay/"
        "trade_level.csv"
    ),
    Path(
        "runtime/current_entry_replay_253/"
        "trade_level.csv"
    ),
]

OUT = Path(
    "runtime/directional_path_rule_all_trades"
)
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# LOAD BEST AVAILABLE REPLAY FILE
# --------------------------------------------------

src = next((p for p in FILES if p.exists()), None)

if src is None:
    raise SystemExit(
        "Could not find a 253-trade replay file."
    )

df = pd.read_csv(src)

print("SOURCE:", src)
print("Rows in source:", len(df))
print("Columns:", list(df.columns))

# Replayable trades only, if status exists.
if "status" in df.columns:
    x = df[
        df["status"].astype(str).str.upper() == "OK"
    ].copy()
else:
    x = df.copy()

print("\nReplayable rows:", len(x))

# --------------------------------------------------
# NORMALIZE FIELDS
# --------------------------------------------------

def to_bool(v):
    return str(v).strip().lower() in {
        "true", "1", "yes", "y"
    }

for c in ["breakout", "pullback"]:
    if c not in x.columns:
        raise SystemExit(
            f"Required column missing: {c}"
        )
    x[c] = x[c].map(to_bool)

x["direction"] = (
    x["direction"]
    .astype(str)
    .str.upper()
)

# Determine which P&L field to use.
pnl_candidates = [
    "sim_net",
    "replay_net",
    "historical_net",
    "historical_net_pnl",
    "net",
    "pnl",
]

PNL_COL = next(
    (c for c in pnl_candidates if c in x.columns),
    None
)

if PNL_COL is None:
    raise SystemExit(
        "Could not identify P&L column."
    )

x["test_pnl"] = pd.to_numeric(
    x[PNL_COL],
    errors="coerce"
)

x = x.dropna(subset=["test_pnl"]).copy()

x["winner"] = x["test_pnl"] > 0

# Find available RVOL column.
rvol_candidates = [
    "relative_volume",
    "relative_volume_ratio",
    "volume_ratio20",
    "rvol",
]

RVOL_COL = next(
    (c for c in rvol_candidates if c in x.columns),
    None
)

if RVOL_COL:
    x["rvol_test"] = pd.to_numeric(
        x[RVOL_COL],
        errors="coerce"
    )
else:
    x["rvol_test"] = np.nan

# Momentum is useful for diagnostics.
momentum_candidates = [
    "momentum_pct",
    "momentum",
    "change_pct",
]

MOM_COL = next(
    (c for c in momentum_candidates if c in x.columns),
    None
)

if MOM_COL:
    x["momentum_test"] = pd.to_numeric(
        x[MOM_COL],
        errors="coerce"
    )
else:
    x["momentum_test"] = np.nan

# --------------------------------------------------
# PATH CLASSIFICATION
# --------------------------------------------------

def path_name(r):
    if r["breakout"] and r["pullback"]:
        return "BREAKOUT_AND_PULLBACK"
    if r["breakout"]:
        return "BREAKOUT_ONLY"
    if r["pullback"]:
        return "PULLBACK_ONLY"
    return "NEITHER"

x["path"] = x.apply(path_name, axis=1)

# Proposed direction-specific rule:
#
# BUY  -> breakout required
# SELL -> pullback required
#
# "Both" therefore passes either relevant side.

x["directional_path_pass"] = (
    (
        (x["direction"] == "BUY")
        &
        x["breakout"]
    )
    |
    (
        (x["direction"] == "SELL")
        &
        x["pullback"]
    )
)

# --------------------------------------------------
# SUMMARY FUNCTION
# --------------------------------------------------

def summarize(name, mask):
    y = x[mask].copy()

    if y.empty:
        return {
            "scenario": name,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "gross_winner_pnl": 0.0,
            "gross_loser_pnl": 0.0,
            "net_pnl": 0.0,
            "avg_pnl": 0.0,
        }

    wins = y[y["test_pnl"] > 0]
    losses = y[y["test_pnl"] <= 0]

    return {
        "scenario": name,
        "trades": len(y),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(y) * 100,
        "gross_winner_pnl":
            wins["test_pnl"].sum(),
        "gross_loser_pnl":
            losses["test_pnl"].sum(),
        "net_pnl":
            y["test_pnl"].sum(),
        "avg_pnl":
            y["test_pnl"].mean(),
    }

# --------------------------------------------------
# BASELINE + PATH RULE
# --------------------------------------------------

scenarios = []

all_mask = pd.Series(
    True,
    index=x.index
)

path_mask = x["directional_path_pass"]

scenarios.append(
    summarize(
        "BASELINE_ALL_REPLAYABLE",
        all_mask
    )
)

scenarios.append(
    summarize(
        "BUY_BREAKOUT__SELL_PULLBACK",
        path_mask
    )
)

# --------------------------------------------------
# RVOL CAPS
# --------------------------------------------------

RVOL_CAPS = [
    1.70,
    1.80,
    1.90,
    2.00,
]

if x["rvol_test"].notna().any():

    for cap in RVOL_CAPS:

        mask = (
            path_mask
            &
            x["rvol_test"].notna()
            &
            (x["rvol_test"] <= cap)
        )

        scenarios.append(
            summarize(
                f"PATH_RULE_RVOL_LE_{cap:.2f}",
                mask
            )
        )

summary = pd.DataFrame(scenarios)

# --------------------------------------------------
# WINNERS / LOSERS REMOVED
# --------------------------------------------------

baseline_winners = x["winner"]
baseline_losers = ~x["winner"]

path_rejected = ~path_mask

old_winners_lost = x[
    baseline_winners & path_rejected
].copy()

old_losers_removed = x[
    baseline_losers & path_rejected
].copy()

print(
    "\n===== FULL DATASET BASELINE ====="
)

print("Trades :", len(x))
print(
    "Wins   :",
    int(x["winner"].sum())
)
print(
    "Losses :",
    int((~x["winner"]).sum())
)
print(
    "Win rate:",
    f"{x['winner'].mean()*100:.1f}%"
)
print(
    "Net P&L:",
    f"Rs {x['test_pnl'].sum():,.2f}"
)
print(
    "Using P&L column:",
    PNL_COL
)

# --------------------------------------------------
# PATH SUMMARY
# --------------------------------------------------

path_summary = (
    x.groupby(
        ["path", "direction"]
    )
    .agg(
        trades=("symbol", "size"),
        wins=("winner", "sum"),
        pnl=("test_pnl", "sum"),
    )
    .reset_index()
)

path_summary["losses"] = (
    path_summary["trades"]
    - path_summary["wins"]
)

path_summary["win_rate_pct"] = (
    path_summary["wins"]
    / path_summary["trades"]
    * 100
)

print(
    "\n===== PATH x DIRECTION "
    "ALL 200+ TRADES ====="
)

print(
    path_summary.to_string(
        index=False,
        formatters={
            "pnl":
                lambda v:
                    f"Rs {v:,.2f}",
            "win_rate_pct":
                lambda v:
                    f"{v:.1f}%",
        }
    )
)

# --------------------------------------------------
# SCENARIO COMPARISON
# --------------------------------------------------

print(
    "\n===== DIRECTIONAL PATH "
    "+ RVOL COMPARISON ====="
)

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v:
                    f"{v:.1f}%",
            "gross_winner_pnl":
                lambda v:
                    f"Rs {v:,.2f}",
            "gross_loser_pnl":
                lambda v:
                    f"Rs {v:,.2f}",
            "net_pnl":
                lambda v:
                    f"Rs {v:,.2f}",
            "avg_pnl":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)

# --------------------------------------------------
# EXACT COST OF FILTER
# --------------------------------------------------

print(
    "\n===== PATH RULE IMPACT ====="
)

print(
    "Historical/replay losers removed :",
    len(old_losers_removed)
)

print(
    "Loss P&L avoided               :",
    f"Rs {old_losers_removed['test_pnl'].sum():,.2f}"
)

print(
    "Historical/replay winners lost  :",
    len(old_winners_lost)
)

print(
    "Winner P&L sacrificed           :",
    f"Rs {old_winners_lost['test_pnl'].sum():,.2f}"
)

delta = (
    -old_losers_removed["test_pnl"].sum()
    -
    old_winners_lost["test_pnl"].sum()
)

print(
    "Net filtering benefit           :",
    f"Rs {delta:,.2f}"
)

# --------------------------------------------------
# RVOL WINNER / LOSER DISTRIBUTION
# --------------------------------------------------

if x["rvol_test"].notna().any():

    print(
        "\n===== RVOL WINNER VS LOSER ====="
    )

    for label, group in x.groupby("winner"):

        name = (
            "WINNERS"
            if label
            else "LOSERS"
        )

        v = group[
            "rvol_test"
        ].dropna()

        if v.empty:
            continue

        print(
            f"{name}: "
            f"n={len(v)} "
            f"mean={v.mean():.4f} "
            f"median={v.median():.4f} "
            f"p25={v.quantile(.25):.4f} "
            f"p75={v.quantile(.75):.4f} "
            f"max={v.max():.4f}"
        )

# --------------------------------------------------
# DETAILED LOSERS THAT SURVIVE PROPOSED RULE
# --------------------------------------------------

remaining_losers = x[
    path_mask
    &
    (~x["winner"])
].copy()

print(
    "\n===== LOSERS STILL ALLOWED "
    "BY DIRECTIONAL PATH RULE ====="
)

detail_cols = [
    c for c in [
        "date",
        "signal_ts",
        "symbol",
        "direction",
        "path",
        "momentum_test",
        "rvol_test",
        "test_pnl",
    ]
    if c in remaining_losers.columns
]

print(
    remaining_losers[
        detail_cols
    ]
    .sort_values(
        "test_pnl"
    )
    .to_string(index=False)
)

# --------------------------------------------------
# WINNERS REMOVED
# --------------------------------------------------

print(
    "\n===== WINNERS REMOVED "
    "BY DIRECTIONAL PATH RULE ====="
)

detail_cols2 = [
    c for c in [
        "date",
        "signal_ts",
        "symbol",
        "direction",
        "path",
        "momentum_test",
        "rvol_test",
        "test_pnl",
    ]
    if c in old_winners_lost.columns
]

print(
    old_winners_lost[
        detail_cols2
    ]
    .sort_values(
        "test_pnl",
        ascending=False
    )
    .to_string(index=False)
)

# --------------------------------------------------
# SAVE
# --------------------------------------------------

x.to_csv(
    OUT / "all_trade_level.csv",
    index=False
)

summary.to_csv(
    OUT / "scenario_comparison.csv",
    index=False
)

path_summary.to_csv(
    OUT / "path_direction_summary.csv",
    index=False
)

remaining_losers.to_csv(
    OUT / "remaining_losers.csv",
    index=False
)

old_losers_removed.to_csv(
    OUT / "losers_removed.csv",
    index=False
)

old_winners_lost.to_csv(
    OUT / "winners_removed.csv",
    index=False
)

print(
    "\nWrote:",
    OUT
)
