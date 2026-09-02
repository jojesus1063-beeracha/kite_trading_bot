from pathlib import Path
import math
import numpy as np
import pandas as pd

SRC = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_stock_direction_test/"
    "feature_level.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "market_sector_stock_direction_walkforward"
)
OUT.mkdir(parents=True, exist_ok=True)

TRAIN_START = "2026-07-29"
TRAIN_END   = "2026-08-10"

TEST_START  = "2026-08-11"
TEST_END    = "2026-08-21"

EXCLUDED_DATE = "2026-07-30"

MIN_CELL_TRADES = 3

# Require some margin of superiority before choosing
# NORMAL or REVERSE instead of SKIP.
MIN_NET_ADVANTAGE = 5.0

# --------------------------------------------------
# LOAD
# --------------------------------------------------

if not SRC.exists():
    raise SystemExit(f"Missing {SRC}")

df = pd.read_csv(SRC)

df["date"] = pd.to_datetime(
    df["date"],
    errors="coerce"
).dt.strftime("%Y-%m-%d")

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

df = df[
    df["date"] != EXCLUDED_DATE
].copy()

df = df.dropna(
    subset=[
        "date",
        "normal_net",
        "reverse_net",
    ]
).copy()

train = df[
    (df["date"] >= TRAIN_START)
    &
    (df["date"] <= TRAIN_END)
].copy()

test = df[
    (df["date"] >= TEST_START)
    &
    (df["date"] <= TEST_END)
].copy()

print("===== WALK-FORWARD SPLIT =====")
print("TRAIN:", TRAIN_START, "->", TRAIN_END)
print("TEST :", TEST_START, "->", TEST_END)
print("Excluded:", EXCLUDED_DATE)
print()
print("Train rows:", len(train))
print("Train dates:", train["date"].nunique())
print("Test rows :", len(test))
print("Test dates:", test["date"].nunique())

GROUP_COLS = [
    "market_trend",
    "sector_trend",
    "stock_trend",
    "direction",
]

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def max_drawdown(values):
    eq = 0.0
    peak = 0.0
    worst = 0.0

    for value in values:
        eq += float(value)
        peak = max(peak, eq)
        worst = min(
            worst,
            eq - peak
        )

    return worst


def metrics(x):
    if x.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "gross": 0.0,
            "costs": 0.0,
            "net": 0.0,
            "avg_net": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "profit_factor": np.nan,
            "max_drawdown": 0.0,
            "profitable_days": 0,
            "losing_days": 0,
        }

    x = x.sort_values(
        ["date", "signal_ts"]
    ).copy()

    wins = x[x["net"] > 0]
    losses = x[x["net"] <= 0]

    gp = float(
        x.loc[
            x["gross"] > 0,
            "gross"
        ].sum()
    )

    gl = float(
        x.loc[
            x["gross"] < 0,
            "gross"
        ].sum()
    )

    pf = (
        gp / abs(gl)
        if gl < 0
        else np.inf
    )

    daily = (
        x.groupby("date")["net"]
        .sum()
    )

    return {
        "trades":
            len(x),

        "wins":
            len(wins),

        "losses":
            len(losses),

        "win_rate_pct":
            len(wins) / len(x) * 100.0,

        "gross_profit":
            gp,

        "gross_loss":
            gl,

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
            if not wins.empty else 0.0,

        "avg_loser":
            float(losses["net"].mean())
            if not losses.empty else 0.0,

        "profit_factor":
            pf,

        "max_drawdown":
            max_drawdown(
                x["net"].tolist()
            ),

        "profitable_days":
            int((daily > 0).sum()),

        "losing_days":
            int((daily < 0).sum()),
    }


def outcome(t, mode):
    if mode == "NORMAL":
        return {
            "gross":
                float(t["normal_gross"]),
            "costs":
                float(t["normal_costs"]),
            "net":
                float(t["normal_net"]),
            "executed_direction":
                str(t["direction"]).upper(),
            "decision":
                "NORMAL",
        }

    return {
        "gross":
            float(t["reverse_gross"]),
        "costs":
            float(t["reverse_costs"]),
        "net":
            float(t["reverse_net"]),
        "executed_direction":
            str(t["reverse_direction"]).upper(),
        "decision":
            "REVERSE",
    }


# --------------------------------------------------
# LEARN TABLE FROM TRAIN ONLY
# --------------------------------------------------

learned_rows = []

for keys, g in train.groupby(
    GROUP_COLS,
    dropna=False,
):

    market, sector, stock, direction = keys

    trades = len(g)

    normal_net = float(
        g["normal_net"].sum()
    )

    reverse_net = float(
        g["reverse_net"].sum()
    )

    normal_wins = int(
        (g["normal_net"] > 0).sum()
    )

    reverse_wins = int(
        (g["reverse_net"] > 0).sum()
    )

    decision = "SKIP"

    if trades >= MIN_CELL_TRADES:

        advantage = abs(
            normal_net - reverse_net
        )

        if advantage >= MIN_NET_ADVANTAGE:

            if (
                normal_net > 0
                and
                normal_net > reverse_net
            ):
                decision = "NORMAL"

            elif (
                reverse_net > 0
                and
                reverse_net > normal_net
            ):
                decision = "REVERSE"

    learned_rows.append({
        "market_trend":
            market,
        "sector_trend":
            sector,
        "stock_trend":
            stock,
        "original_direction":
            direction,

        "train_trades":
            trades,

        "train_normal_wins":
            normal_wins,

        "train_normal_net":
            normal_net,

        "train_reverse_wins":
            reverse_wins,

        "train_reverse_net":
            reverse_net,

        "train_net_advantage":
            abs(
                normal_net -
                reverse_net
            ),

        "decision":
            decision,
    })

table = pd.DataFrame(
    learned_rows
)

table.to_csv(
    OUT / "learned_table.csv",
    index=False
)

print(
    "\n===== LEARNED TABLE — TRAIN ONLY ====="
)

print(
    table.sort_values(
        "train_trades",
        ascending=False
    ).to_string(
        index=False,
        formatters={
            "train_normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "train_reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "train_net_advantage":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)

# --------------------------------------------------
# LOOKUP
# --------------------------------------------------

lookup = {}

for _, r in table.iterrows():

    key = (
        r["market_trend"],
        r["sector_trend"],
        r["stock_trend"],
        r["original_direction"],
    )

    lookup[key] = {
        "decision":
            r["decision"],
        "train_trades":
            r["train_trades"],
        "train_normal_net":
            r["train_normal_net"],
        "train_reverse_net":
            r["train_reverse_net"],
    }


# --------------------------------------------------
# APPLY FROZEN TABLE TO TEST
# --------------------------------------------------

executed_rows = []
test_audit = []

for _, t in test.iterrows():

    key = (
        t["market_trend"],
        t["sector_trend"],
        t["stock_trend"],
        t["direction"],
    )

    rule = lookup.get(
        key
    )

    if rule is None:
        decision = "SKIP_UNSEEN"
    else:
        decision = rule[
            "decision"
        ]

    audit = {
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "symbol":
            t["symbol"],
        "sector":
            t["sector"],
        "market_trend":
            t["market_trend"],
        "sector_trend":
            t["sector_trend"],
        "stock_trend":
            t["stock_trend"],
        "original_direction":
            t["direction"],
        "decision":
            decision,
        "normal_net":
            t["normal_net"],
        "reverse_net":
            t["reverse_net"],
    }

    if rule is not None:
        audit.update({
            "train_cell_trades":
                rule["train_trades"],
            "train_cell_normal_net":
                rule["train_normal_net"],
            "train_cell_reverse_net":
                rule["train_reverse_net"],
        })

    test_audit.append(
        audit
    )

    if decision not in {
        "NORMAL",
        "REVERSE",
    }:
        continue

    o = outcome(
        t,
        decision
    )

    executed_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "symbol":
            t["symbol"],
        "sector":
            t["sector"],

        "market_trend":
            t["market_trend"],
        "sector_trend":
            t["sector_trend"],
        "stock_trend":
            t["stock_trend"],

        "original_direction":
            t["direction"],

        **o,
    })

executed = pd.DataFrame(
    executed_rows
)

audit_df = pd.DataFrame(
    test_audit
)

audit_df.to_csv(
    OUT / "test_audit.csv",
    index=False
)

executed.to_csv(
    OUT / "test_executed.csv",
    index=False
)


# --------------------------------------------------
# TEST PERFORMANCE
# --------------------------------------------------

m = metrics(
    executed
)

print(
    "\n===== TRUE OUT-OF-SAMPLE TEST RESULT ====="
)

for k, v in m.items():

    if isinstance(v, float):
        print(
            f"{k:20s}: {v:.2f}"
        )
    else:
        print(
            f"{k:20s}: {v}"
        )

print(
    "Test candidates       :",
    len(test)
)

print(
    "Executed              :",
    len(executed)
)

print(
    "Skipped               :",
    len(test)-len(executed)
)

print(
    "SKIP_UNSEEN           :",
    int(
        (
            audit_df["decision"]
            ==
            "SKIP_UNSEEN"
        ).sum()
    )
)

print(
    "SKIP learned          :",
    int(
        (
            audit_df["decision"]
            ==
            "SKIP"
        ).sum()
    )
)


# --------------------------------------------------
# BASELINE BENCHMARKS ON SAME TEST PERIOD
# --------------------------------------------------

baseline_rows = []
all_reverse_rows = []
reverse_buy_rows = []

for _, t in test.iterrows():

    n = outcome(
        t,
        "NORMAL"
    )

    baseline_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            n["gross"],
        "costs":
            n["costs"],
        "net":
            n["net"],
    })

    r = outcome(
        t,
        "REVERSE"
    )

    all_reverse_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            r["gross"],
        "costs":
            r["costs"],
        "net":
            r["net"],
    })

    if (
        str(t["direction"]).upper()
        ==
        "BUY"
    ):
        chosen = r
    else:
        chosen = n

    reverse_buy_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            chosen["gross"],
        "costs":
            chosen["costs"],
        "net":
            chosen["net"],
    })


benchmarks = []

for name, frame in [
    (
        "CURRENT_NORMAL",
        pd.DataFrame(
            baseline_rows
        )
    ),
    (
        "ALL_REVERSE",
        pd.DataFrame(
            all_reverse_rows
        )
    ),
    (
        "REVERSE_BUY_ONLY",
        pd.DataFrame(
            reverse_buy_rows
        )
    ),
    (
        "FROZEN_DIRECTION_TABLE",
        executed
    ),
]:

    mm = metrics(
        frame
    )

    benchmarks.append({
        "strategy":
            name,
        **mm,
    })

benchmark_df = pd.DataFrame(
    benchmarks
)

benchmark_df.to_csv(
    OUT / "test_benchmarks.csv",
    index=False
)

print(
    "\n===== TEST-PERIOD BENCHMARKS ====="
)

print(
    benchmark_df.to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda v:
                    f"{v:.1f}%",
            "gross":
                lambda v:
                    f"Rs {v:,.2f}",
            "costs":
                lambda v:
                    f"Rs {v:,.2f}",
            "net":
                lambda v:
                    f"Rs {v:,.2f}",
            "avg_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "avg_winner":
                lambda v:
                    f"Rs {v:,.2f}",
            "avg_loser":
                lambda v:
                    f"Rs {v:,.2f}",
            "profit_factor":
                lambda v:
                    (
                        "INF"
                        if math.isinf(v)
                        else
                        f"{v:.2f}"
                        if pd.notna(v)
                        else "NA"
                    ),
            "max_drawdown":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)


# --------------------------------------------------
# DAYWISE OUT-OF-SAMPLE
# --------------------------------------------------

if not executed.empty:

    day = (
        executed.groupby("date")
        .agg(
            trades=("symbol", "size"),

            normal_trades=(
                "decision",
                lambda s:
                    int(
                        (
                            s == "NORMAL"
                        ).sum()
                    )
            ),

            reverse_trades=(
                "decision",
                lambda s:
                    int(
                        (
                            s == "REVERSE"
                        ).sum()
                    )
            ),

            wins=(
                "net",
                lambda s:
                    int(
                        (
                            s > 0
                        ).sum()
                    )
            ),

            losses=(
                "net",
                lambda s:
                    int(
                        (
                            s <= 0
                        ).sum()
                    )
            ),

            gross=(
                "gross",
                "sum"
            ),

            costs=(
                "costs",
                "sum"
            ),

            net=(
                "net",
                "sum"
            ),
        )
        .reset_index()
    )

    day["cumulative_net"] = (
        day["net"].cumsum()
    )

else:

    day = pd.DataFrame()

day.to_csv(
    OUT / "test_daywise.csv",
    index=False
)

print(
    "\n===== OUT-OF-SAMPLE DAYWISE ====="
)

if day.empty:
    print("None")
else:
    print(
        day.to_string(
            index=False,
            formatters={
                "gross":
                    lambda v:
                        f"Rs {v:,.2f}",
                "costs":
                    lambda v:
                        f"Rs {v:,.2f}",
                "net":
                    lambda v:
                        f"Rs {v:,.2f}",
                "cumulative_net":
                    lambda v:
                        f"Rs {v:,.2f}",
            }
        )
    )


# --------------------------------------------------
# DECISION PERFORMANCE
# --------------------------------------------------

if not executed.empty:

    decision_perf = (
        executed.groupby(
            "decision"
        )
        .agg(
            trades=(
                "symbol",
                "size"
            ),
            wins=(
                "net",
                lambda s:
                    int(
                        (
                            s > 0
                        ).sum()
                    )
            ),
            losses=(
                "net",
                lambda s:
                    int(
                        (
                            s <= 0
                        ).sum()
                    )
            ),
            net=(
                "net",
                "sum"
            ),
        )
        .reset_index()
    )

    decision_perf[
        "win_rate_pct"
    ] = (
        decision_perf["wins"]
        /
        decision_perf["trades"]
        * 100
    )

else:
    decision_perf = pd.DataFrame()

print(
    "\n===== NORMAL VS REVERSE COMPONENT — TEST ====="
)

if decision_perf.empty:
    print("None")
else:
    print(
        decision_perf.to_string(
            index=False,
            formatters={
                "net":
                    lambda v:
                        f"Rs {v:,.2f}",
                "win_rate_pct":
                    lambda v:
                        f"{v:.1f}%",
            }
        )
    )


# --------------------------------------------------
# CELL-BY-CELL TEST AUDIT
# --------------------------------------------------

cell_rows = []

for keys, g in audit_df.groupby(
    [
        "market_trend",
        "sector_trend",
        "stock_trend",
        "original_direction",
        "decision",
    ],
    dropna=False,
):

    market, sector, stock, direction, decision = keys

    cell_rows.append({
        "market_trend":
            market,
        "sector_trend":
            sector,
        "stock_trend":
            stock,
        "original_direction":
            direction,
        "decision":
            decision,

        "test_candidates":
            len(g),

        "test_normal_net":
            g["normal_net"].sum(),

        "test_reverse_net":
            g["reverse_net"].sum(),
    })

cell_test = pd.DataFrame(
    cell_rows
)

cell_test.to_csv(
    OUT / "test_cell_audit.csv",
    index=False
)

print(
    "\n===== CELL-BY-CELL OUT-OF-SAMPLE AUDIT ====="
)

print(
    cell_test.sort_values(
        "test_candidates",
        ascending=False
    ).to_string(
        index=False,
        formatters={
            "test_normal_net":
                lambda v:
                    f"Rs {v:,.2f}",
            "test_reverse_net":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)


print(
    "\n===== IMPORTANT ====="
)

print(
    "Decision table learned ONLY from:",
    TRAIN_START,
    "to",
    TRAIN_END
)

print(
    "Decision table frozen BEFORE:",
    TEST_START
)

print(
    "No test-period outcome was used "
    "to choose NORMAL/REVERSE/SKIP."
)

print(
    "\nWrote:",
    OUT
)
