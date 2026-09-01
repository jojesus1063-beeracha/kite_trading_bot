from pathlib import Path
import pandas as pd
import numpy as np

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/"
    "feature_comparison.csv"
)

TRADES = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_timeframe_comparison/"
    "trades_3m.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "nifty_sideways_skip_test"
)
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(BASE)

# --------------------------------------------------
# CLEAN TYPES
# --------------------------------------------------

for c in [
    "normal_net", "reverse_net",
    "normal_gross", "reverse_gross",
    "normal_costs", "reverse_costs"
]:
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

df["date"] = pd.to_datetime(
    df["date"], errors="coerce"
).dt.strftime("%Y-%m-%d")

# Use the actual 3-minute market/sector classifications.
df["test_market"] = df["3M_market"].astype(str).str.upper()
df["test_sector"] = df["3M_sector"].astype(str).str.upper()
df["test_stock"] = df["stock_trend"].astype(str).str.upper()

# --------------------------------------------------
# LOAD THE ACTUAL TRADES SELECTED BY THE EXISTING
# 3M/3M/3M TEST.
#
# This is important: we are NOT creating a new
# candidate population.
# --------------------------------------------------

taken = pd.read_csv(TRADES)

taken["date"] = pd.to_datetime(
    taken["date"], errors="coerce"
).dt.strftime("%Y-%m-%d")

taken["signal_ts"] = pd.to_datetime(
    taken["signal_ts"], errors="coerce"
)

df["signal_ts_dt"] = pd.to_datetime(
    df["signal_ts"], errors="coerce"
)

taken_keys = set(
    zip(
        taken["date"],
        taken["signal_ts"],
        taken["symbol"].astype(str)
    )
)

df["was_3m_trade"] = [
    (
        d,
        ts,
        str(sym)
    ) in taken_keys
    for d, ts, sym in zip(
        df["date"],
        df["signal_ts_dt"],
        df["symbol"]
    )
]

base = df[df["was_3m_trade"]].copy()

print("===== SOURCE CHECK =====")
print("trades_3m.csv rows :", len(taken))
print("Matched feature rows:", len(base))

# Duplicate protection
if len(base) != len(taken):
    print(
        "WARNING: feature rows do not match "
        "trades_3m.csv exactly."
    )

# --------------------------------------------------
# IDENTIFY WHETHER THE EXISTING 3M TEST CHOSE
# NORMAL OR REVERSE.
#
# Match its recorded net result against normal_net /
# reverse_net.
# --------------------------------------------------

lookup = taken.copy()

lookup = lookup.rename(
    columns={
        "decision": "old_decision",
        "gross": "old_gross",
        "costs": "old_costs",
        "net": "old_net",
    }
)

lookup["signal_ts_dt"] = pd.to_datetime(
    lookup["signal_ts"], errors="coerce"
)

base = base.merge(
    lookup[
        [
            "date",
            "signal_ts_dt",
            "symbol",
            "old_decision",
            "old_gross",
            "old_costs",
            "old_net",
        ]
    ],
    on=["date", "signal_ts_dt", "symbol"],
    how="inner",
)

# --------------------------------------------------
# NEW RULE
#
# ONLY change:
#
# NIFTY SIDEWAYS / UNKNOWN -> SKIP
#
# Everything else retains the ORIGINAL 3M decision.
# --------------------------------------------------

def new_decision(row):
    market = row["test_market"]

    if market in {
        "SIDEWAYS",
        "UNKNOWN",
        "NAN",
        "",
        "NONE",
    }:
        return "SKIP"

    return str(row["old_decision"]).upper()


base["new_decision"] = base.apply(
    new_decision,
    axis=1
)

# Preserve original outcome for trades that remain.
base["new_gross"] = np.where(
    base["new_decision"] == "SKIP",
    0.0,
    pd.to_numeric(base["old_gross"], errors="coerce")
)

base["new_costs"] = np.where(
    base["new_decision"] == "SKIP",
    0.0,
    pd.to_numeric(base["old_costs"], errors="coerce")
)

base["new_net"] = np.where(
    base["new_decision"] == "SKIP",
    0.0,
    pd.to_numeric(base["old_net"], errors="coerce")
)

# --------------------------------------------------
# SUMMARY FUNCTION
# --------------------------------------------------

def summarize(name, x, pnl_col, gross_col, costs_col):
    executed = x.copy()

    if name == "NEW_SIDEWAYS_SKIP":
        executed = executed[
            executed["new_decision"] != "SKIP"
        ]

    pnl = pd.to_numeric(
        executed[pnl_col],
        errors="coerce"
    ).fillna(0)

    gross = pd.to_numeric(
        executed[gross_col],
        errors="coerce"
    ).fillna(0)

    costs = pd.to_numeric(
        executed[costs_col],
        errors="coerce"
    ).fillna(0)

    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    trades = len(executed)

    return {
        "strategy": name,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (
            wins / trades * 100
            if trades else 0
        ),
        "gross": gross.sum(),
        "costs": costs.sum(),
        "net": pnl.sum(),
        "avg_net": (
            pnl.mean()
            if trades else 0
        ),
        "skipped": (
            0
            if name == "CURRENT_3M"
            else int(
                (x["new_decision"] == "SKIP").sum()
            )
        ),
    }


summary = pd.DataFrame([
    summarize(
        "CURRENT_3M",
        base,
        "old_net",
        "old_gross",
        "old_costs"
    ),
    summarize(
        "NEW_SIDEWAYS_SKIP",
        base,
        "new_net",
        "new_gross",
        "new_costs"
    ),
])

print("\n===== COMPARISON =====")
print(summary.to_string(index=False))

# --------------------------------------------------
# WHAT DID THE NEW RULE REMOVE?
# --------------------------------------------------

removed = base[
    base["new_decision"] == "SKIP"
].copy()

removed["removed_winner"] = (
    pd.to_numeric(
        removed["old_net"],
        errors="coerce"
    ) > 0
)

removed["removed_loser"] = (
    pd.to_numeric(
        removed["old_net"],
        errors="coerce"
    ) < 0
)

print("\n===== REMOVED TRADES =====")

cols = [
    "date",
    "signal_ts",
    "symbol",
    "direction",
    "old_decision",
    "test_market",
    "test_sector",
    "test_stock",
    "old_net",
]

cols = [c for c in cols if c in removed.columns]

if len(removed):
    print(
        removed[cols]
        .sort_values(["date", "signal_ts"])
        .to_string(index=False)
    )
else:
    print("None")

print("\n===== REMOVAL EFFECT =====")

removed_winners = removed[
    removed["removed_winner"]
]

removed_losers = removed[
    removed["removed_loser"]
]

print(
    "Winners removed :",
    len(removed_winners),
    "P&L =",
    round(removed_winners["old_net"].sum(), 2)
)

print(
    "Losers removed  :",
    len(removed_losers),
    "P&L =",
    round(removed_losers["old_net"].sum(), 2)
)

print(
    "Net P&L avoided :",
    round(
        -removed["old_net"].sum(),
        2
    )
)

# --------------------------------------------------
# DAYWISE NEW RESULT
# --------------------------------------------------

executed = base[
    base["new_decision"] != "SKIP"
].copy()

rows = []

for date, g in executed.groupby("date"):
    pnl = pd.to_numeric(
        g["new_net"],
        errors="coerce"
    ).fillna(0)

    rows.append({
        "date": date,
        "trades": len(g),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "net": pnl.sum(),
    })

daywise = pd.DataFrame(rows)

if not daywise.empty:
    daywise["cumulative_net"] = (
        daywise["net"].cumsum()
    )

print("\n===== NEW DAYWISE =====")

if len(daywise):
    print(daywise.to_string(index=False))
else:
    print("No trades.")

# --------------------------------------------------
# SPECIFIC THREE LOSERS
# --------------------------------------------------

print("\n===== THREE KNOWN LOSERS =====")

known = base[
    (
        (
            (base["date"] == "2026-08-11")
            &
            (base["symbol"].isin([
                "BANDHANBNK",
                "CIEINDIA",
            ]))
        )
        |
        (
            (base["date"] == "2026-08-21")
            &
            (base["symbol"] == "KRONOX")
        )
    )
    &
    (
        pd.to_numeric(
            base["old_net"],
            errors="coerce"
        ) < 0
    )
]

show = [
    "date",
    "signal_ts",
    "symbol",
    "direction",
    "test_market",
    "test_sector",
    "test_stock",
    "old_decision",
    "new_decision",
    "old_net",
]

show = [c for c in show if c in known.columns]

print(
    known[show]
    .sort_values(["date", "signal_ts"])
    .to_string(index=False)
)

# --------------------------------------------------
# SAVE
# --------------------------------------------------

summary.to_csv(
    OUT / "summary.csv",
    index=False
)

removed.to_csv(
    OUT / "removed_trades.csv",
    index=False
)

daywise.to_csv(
    OUT / "daywise.csv",
    index=False
)

base.to_csv(
    OUT / "trade_level.csv",
    index=False
)

print("\nWrote:", OUT)
