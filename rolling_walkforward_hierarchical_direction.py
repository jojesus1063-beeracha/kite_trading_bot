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
    "rolling_walkforward_hierarchical_direction"
)
OUT.mkdir(parents=True, exist_ok=True)

EXCLUDED_DATE = "2026-07-30"

# Initial training ends here.
INITIAL_TRAIN_END = "2026-08-10"

# Minimum history required at each hierarchy level.
MIN_EXACT = 3
MIN_MARKET_STOCK = 4
MIN_STOCK_DIRECTION = 5

# Require meaningful historical difference between normal/reverse.
MIN_NET_ADVANTAGE = 5.0

# --------------------------------------------------
# LOAD
# --------------------------------------------------

if not SRC.exists():
    raise SystemExit(f"Missing: {SRC}")

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
        "signal_ts",
        "normal_net",
        "reverse_net",
        "market_trend",
        "stock_trend",
        "direction",
    ]
).copy()

df = df.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

all_dates = sorted(
    df["date"].unique()
)

test_dates = [
    d for d in all_dates
    if d > INITIAL_TRAIN_END
]

print("===== ROLLING WALK-FORWARD SETUP =====")
print("Rows:", len(df))
print("Dates:", len(all_dates))
print("Initial train through:", INITIAL_TRAIN_END)
print("Test dates:", test_dates)
print("Excluded:", EXCLUDED_DATE)


# --------------------------------------------------
# METRICS
# --------------------------------------------------

def max_drawdown(values):
    eq = 0.0
    peak = 0.0
    worst = 0.0

    for v in values:
        eq += float(v)
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

    day_net = x.groupby(
        "date"
    )["net"].sum()

    return {
        "trades":
            len(x),

        "wins":
            len(wins),

        "losses":
            len(losses),

        "win_rate_pct":
            len(wins) / len(x) * 100.0,

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
            int((day_net > 0).sum()),

        "losing_days":
            int((day_net < 0).sum()),
    }


def choose_from_group(g, minimum):
    if g is None or len(g) < minimum:
        return None

    normal_net = float(
        g["normal_net"].sum()
    )

    reverse_net = float(
        g["reverse_net"].sum()
    )

    advantage = abs(
        normal_net - reverse_net
    )

    if advantage < MIN_NET_ADVANTAGE:
        return None

    if (
        normal_net > 0
        and
        normal_net > reverse_net
    ):
        return {
            "decision": "NORMAL",
            "history_trades": len(g),
            "history_normal_net": normal_net,
            "history_reverse_net": reverse_net,
        }

    if (
        reverse_net > 0
        and
        reverse_net > normal_net
    ):
        return {
            "decision": "REVERSE",
            "history_trades": len(g),
            "history_normal_net": normal_net,
            "history_reverse_net": reverse_net,
        }

    return None


def get_decision(train, t):
    # LEVEL 1:
    # Market + Sector + Stock + Direction
    exact = train[
        (train["market_trend"] == t["market_trend"])
        &
        (train["sector_trend"] == t["sector_trend"])
        &
        (train["stock_trend"] == t["stock_trend"])
        &
        (train["direction"] == t["direction"])
    ]

    r = choose_from_group(
        exact,
        MIN_EXACT
    )

    if r is not None:
        r["level"] = "EXACT"
        return r

    # LEVEL 2:
    # Market + Stock + Direction
    ms = train[
        (train["market_trend"] == t["market_trend"])
        &
        (train["stock_trend"] == t["stock_trend"])
        &
        (train["direction"] == t["direction"])
    ]

    r = choose_from_group(
        ms,
        MIN_MARKET_STOCK
    )

    if r is not None:
        r["level"] = "MARKET_STOCK"
        return r

    # LEVEL 3:
    # Stock + Direction
    sd = train[
        (train["stock_trend"] == t["stock_trend"])
        &
        (train["direction"] == t["direction"])
    ]

    r = choose_from_group(
        sd,
        MIN_STOCK_DIRECTION
    )

    if r is not None:
        r["level"] = "STOCK_DIRECTION"
        return r

    return {
        "decision": "SKIP",
        "level": "NO_RULE",
        "history_trades": 0,
        "history_normal_net": 0.0,
        "history_reverse_net": 0.0,
    }


def outcome(t, decision):
    if decision == "NORMAL":
        return {
            "gross":
                float(t["normal_gross"]),
            "costs":
                float(t["normal_costs"]),
            "net":
                float(t["normal_net"]),
            "executed_direction":
                str(t["direction"]).upper(),
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
    }


# --------------------------------------------------
# ROLLING WALK-FORWARD
# --------------------------------------------------

executed_rows = []
audit_rows = []
daily_rows = []

for test_date in test_dates:

    train = df[
        df["date"] < test_date
    ].copy()

    today = df[
        df["date"] == test_date
    ].copy()

    before_today = len(executed_rows)

    for _, t in today.iterrows():

        rule = get_decision(
            train,
            t
        )

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
                rule["decision"],
            "hierarchy_level":
                rule["level"],
            "history_trades":
                rule["history_trades"],
            "history_normal_net":
                rule["history_normal_net"],
            "history_reverse_net":
                rule["history_reverse_net"],
            "normal_net":
                t["normal_net"],
            "reverse_net":
                t["reverse_net"],
        }

        audit_rows.append(
            audit
        )

        if rule["decision"] == "SKIP":
            continue

        o = outcome(
            t,
            rule["decision"]
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
            "decision":
                rule["decision"],
            "hierarchy_level":
                rule["level"],
            "history_trades":
                rule["history_trades"],
            **o,
        })

    today_exec = pd.DataFrame(
        executed_rows[
            before_today:
        ]
    )

    mm = metrics(
        today_exec
    )

    daily_rows.append({
        "date":
            test_date,
        "candidates":
            len(today),
        "executed":
            len(today_exec),
        "skipped":
            len(today)-len(today_exec),
        **mm,
    })


executed = pd.DataFrame(
    executed_rows
)

audit = pd.DataFrame(
    audit_rows
)

daily = pd.DataFrame(
    daily_rows
)

executed.to_csv(
    OUT / "executed_trade_level.csv",
    index=False
)

audit.to_csv(
    OUT / "decision_audit.csv",
    index=False
)

daily.to_csv(
    OUT / "daywise.csv",
    index=False
)


# --------------------------------------------------
# BENCHMARKS ON SAME ROLLING TEST PERIOD
# --------------------------------------------------

test = df[
    df["date"].isin(
        test_dates
    )
].copy()

baseline_rows = []
reverse_all_rows = []
reverse_buy_rows = []

for _, t in test.iterrows():

    baseline_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            t["normal_gross"],
        "costs":
            t["normal_costs"],
        "net":
            t["normal_net"],
    })

    reverse_all_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            t["reverse_gross"],
        "costs":
            t["reverse_costs"],
        "net":
            t["reverse_net"],
    })

    if (
        str(t["direction"]).upper()
        ==
        "BUY"
    ):
        g = t["reverse_gross"]
        c = t["reverse_costs"]
        n = t["reverse_net"]
    else:
        g = t["normal_gross"]
        c = t["normal_costs"]
        n = t["normal_net"]

    reverse_buy_rows.append({
        "date":
            t["date"],
        "signal_ts":
            t["signal_ts"],
        "gross":
            g,
        "costs":
            c,
        "net":
            n,
    })


benchmark_rows = []

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
            reverse_all_rows
        )
    ),
    (
        "REVERSE_BUY_ONLY",
        pd.DataFrame(
            reverse_buy_rows
        )
    ),
    (
        "HIERARCHICAL_ROLLING",
        executed
    ),
]:

    mm = metrics(frame)

    benchmark_rows.append({
        "strategy":
            name,
        **mm,
    })

bench = pd.DataFrame(
    benchmark_rows
)

bench.to_csv(
    OUT / "benchmarks.csv",
    index=False
)


# --------------------------------------------------
# HIERARCHY LEVEL PERFORMANCE
# --------------------------------------------------

if not executed.empty:

    level_perf = (
        executed.groupby(
            "hierarchy_level"
        )
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
                    int((s <= 0).sum())
            ),
            net=("net", "sum"),
        )
        .reset_index()
    )

    level_perf[
        "win_rate_pct"
    ] = (
        level_perf["wins"]
        /
        level_perf["trades"]
        * 100
    )

else:
    level_perf = pd.DataFrame()

level_perf.to_csv(
    OUT / "hierarchy_level_performance.csv",
    index=False
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
            trades=("symbol", "size"),
            wins=(
                "net",
                lambda s:
                    int((s > 0).sum())
            ),
            losses=(
                "net",
                lambda s:
                    int((s <= 0).sum())
            ),
            net=("net", "sum"),
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

decision_perf.to_csv(
    OUT / "decision_performance.csv",
    index=False
)


# --------------------------------------------------
# PRINT
# --------------------------------------------------

print(
    "\n===== ROLLING WALK-FORWARD RESULT ====="
)

overall = metrics(
    executed
)

for k, v in overall.items():
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
    "\n===== BENCHMARKS ====="
)

print(
    bench.to_string(
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
                        else f"{v:.2f}"
                        if pd.notna(v)
                        else "NA"
                    ),
            "max_drawdown":
                lambda v:
                    f"Rs {v:,.2f}",
        }
    )
)


print(
    "\n===== ROLLING DAYWISE ====="
)

if daily.empty:
    print("None")
else:
    daily["cumulative_net"] = (
        daily["net"].cumsum()
    )

    print(
        daily.to_string(
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
                "max_drawdown":
                    lambda v:
                        f"Rs {v:,.2f}",
                "cumulative_net":
                    lambda v:
                        f"Rs {v:,.2f}",
            }
        )
    )


print(
    "\n===== HIERARCHY LEVEL PERFORMANCE ====="
)

if level_perf.empty:
    print("None")
else:
    print(
        level_perf.to_string(
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


print(
    "\n===== NORMAL VS REVERSE PERFORMANCE ====="
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


print(
    "\n===== DECISION COVERAGE ====="
)

print(
    audit["decision"]
    .value_counts()
    .to_string()
)

print(
    "\nHierarchy usage:"
)

print(
    audit["hierarchy_level"]
    .value_counts()
    .to_string()
)

print(
    "\nWrote:",
    OUT
)
