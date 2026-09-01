from pathlib import Path
import pandas as pd

TRADE_FILE = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_ranked_watchlist/"
    "rpt_sweep_trade_level.csv"
)

RPT = 0.20

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "breakout_vs_pullback_analysis"
)
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(TRADE_FILE)

df["rpt_pct"] = pd.to_numeric(
    df["rpt_pct"],
    errors="coerce"
)

x = df[
    df["rpt_pct"].round(6) == RPT
].copy()

if x.empty:
    raise SystemExit("No RPT 0.20 trades found")

for c in ["breakout", "pullback"]:
    x[c] = (
        x[c]
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes"])
    )

x["winner"] = (
    pd.to_numeric(x["net"], errors="coerce") > 0
)

def path(row):
    if row["breakout"] and row["pullback"]:
        return "BREAKOUT_AND_PULLBACK"
    if row["breakout"]:
        return "BREAKOUT_ONLY"
    if row["pullback"]:
        return "PULLBACK_ONLY"
    return "OTHER"

x["path"] = x.apply(path, axis=1)

print("===== BASELINE =====")
print("Trades :", len(x))
print("Wins   :", int(x["winner"].sum()))
print("Losses :", int((~x["winner"]).sum()))
print("Net    :", f"Rs {x['net'].sum():,.2f}")

print("\n===== PATH SUMMARY =====")

summary = (
    x.groupby("path")
    .agg(
        trades=("symbol", "size"),
        wins=("winner", "sum"),
        gross=("gross", "sum"),
        costs=("costs", "sum"),
        net=("net", "sum"),
    )
    .reset_index()
)

summary["losses"] = (
    summary["trades"] - summary["wins"]
)

summary["win_rate_pct"] = (
    summary["wins"] / summary["trades"] * 100
)

print(
    summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v: f"{v:.1f}%",
            "gross":
                lambda v: f"Rs {v:,.2f}",
            "costs":
                lambda v: f"Rs {v:,.2f}",
            "net":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

print("\n===== WINNERS BY PATH =====")

w = x[x["winner"]].copy()

print(
    w[
        [
            "date",
            "symbol",
            "direction",
            "path",
            "entry",
            "qty",
            "gross",
            "costs",
            "net",
        ]
    ]
    .sort_values("net", ascending=False)
    .to_string(index=False)
)

print("\n===== LOSERS BY PATH =====")

l = x[~x["winner"]].copy()

print(
    l[
        [
            "date",
            "symbol",
            "direction",
            "path",
            "entry",
            "qty",
            "gross",
            "costs",
            "net",
        ]
    ]
    .sort_values("net")
    .to_string(index=False)
)

print("\n===== PATH x DIRECTION =====")

pd_summary = (
    x.groupby(["path", "direction"])
    .agg(
        trades=("symbol", "size"),
        wins=("winner", "sum"),
        net=("net", "sum"),
    )
    .reset_index()
)

pd_summary["losses"] = (
    pd_summary["trades"] - pd_summary["wins"]
)

pd_summary["win_rate_pct"] = (
    pd_summary["wins"]
    / pd_summary["trades"]
    * 100
)

print(
    pd_summary.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v: f"{v:.1f}%",
            "net":
                lambda v: f"Rs {v:,.2f}",
        }
    )
)

# --------------------------------------------------
# Pull selected signal-time attributes if present
# --------------------------------------------------

candidate_cols = [
    "momentum_pct",
    "relative_volume",
    "rank",
    "score",
    "entry",
    "qty",
    "proposed_risk",
]

available = [
    c for c in candidate_cols
    if c in x.columns
]

print("\n===== WINNER VS LOSER VALUES =====")

for col in available:
    vals = pd.to_numeric(
        x[col],
        errors="coerce"
    )

    tmp = pd.DataFrame({
        "value": vals,
        "winner": x["winner"],
    }).dropna()

    if tmp.empty:
        continue

    print(f"\n{col}")

    for label, g in tmp.groupby("winner"):
        name = "WINNERS" if label else "LOSERS"

        print(
            f"  {name}: "
            f"n={len(g)} "
            f"mean={g['value'].mean():.4f} "
            f"median={g['value'].median():.4f} "
            f"min={g['value'].min():.4f} "
            f"max={g['value'].max():.4f}"
        )

x.to_csv(
    OUT / "trade_level_with_path.csv",
    index=False
)

summary.to_csv(
    OUT / "path_summary.csv",
    index=False
)

pd_summary.to_csv(
    OUT / "path_direction_summary.csv",
    index=False
)

print("\nWrote:", OUT)
