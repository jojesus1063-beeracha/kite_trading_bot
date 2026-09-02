from pathlib import Path
import math
import numpy as np
import pandas as pd

EXCLUDED_DATE = "2026-07-30"

BASE = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test/"
    "base_normal_reverse.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test/"
    "excl_2026_07_30"
)
OUT.mkdir(parents=True, exist_ok=True)

ADX_LEVELS = [15, 20, 25, 30, 35, 40]
BUY_CLV_LEVELS = [0.55, 0.60, 0.65, 0.70, 0.75]
VOLUME_LEVELS = [0.8, 1.0, 1.2, 1.5, 2.0]

if not BASE.exists():
    raise SystemExit(f"Missing {BASE}")

df = pd.read_csv(BASE)

df["date"] = pd.to_datetime(
    df["date"], errors="coerce"
).dt.strftime("%Y-%m-%d")

df["signal_ts"] = pd.to_datetime(
    df["signal_ts"], errors="coerce"
)

before = len(df)

df = df[
    df["date"] != EXCLUDED_DATE
].copy()

df = df.sort_values(
    ["date", "signal_ts"]
).reset_index(drop=True)

print("===== DATE EXCLUSION =====")
print("Rows before   :", before)
print("Rows removed  :", before - len(df))
print("Rows remaining:", len(df))


# --------------------------------------------------
# NORMALIZE NUMERICS
# --------------------------------------------------

NUMERIC = [
    "adx",
    "plus_di",
    "minus_di",
    "clv01",
    "volume_ratio",
    "normal_gross",
    "normal_costs",
    "normal_net",
    "reverse_gross",
    "reverse_costs",
    "reverse_net",
]

for c in NUMERIC:
    df[c] = pd.to_numeric(
        df[c], errors="coerce"
    )

df = df.dropna(
    subset=[
        "adx",
        "plus_di",
        "minus_di",
        "clv01",
        "volume_ratio",
        "normal_net",
        "reverse_net",
    ]
).copy()


# --------------------------------------------------
# METRICS
# --------------------------------------------------

def max_drawdown(values):
    equity = 0.0
    peak = 0.0
    worst = 0.0

    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        worst = min(
            worst,
            equity - peak
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

    wins = x[x["net"] > 0]
    losses = x[x["net"] <= 0]

    gp = float(
        x.loc[x["gross"] > 0, "gross"].sum()
    )

    gl = float(
        x.loc[x["gross"] < 0, "gross"].sum()
    )

    pf = (
        gp / abs(gl)
        if gl < 0
        else np.inf
    )

    daily = x.groupby("date")["net"].sum()

    return {
        "trades": len(x),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct":
            len(wins) / len(x) * 100.0,
        "gross_profit": gp,
        "gross_loss": gl,
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
        "profit_factor": pf,
        "max_drawdown":
            max_drawdown(
                x["net"].tolist()
            ),
        "profitable_days":
            int((daily > 0).sum()),
        "losing_days":
            int((daily < 0).sum()),
    }


def row_from_side(t, use_reverse):
    if use_reverse:
        return {
            "date": t["date"],
            "signal_ts": t["signal_ts"],
            "symbol": t["symbol"],
            "original_direction":
                t["direction"],
            "executed_direction":
                t["reverse_direction"],
            "normal_or_reverse":
                "REVERSE",
            "gross":
                float(t["reverse_gross"]),
            "costs":
                float(t["reverse_costs"]),
            "net":
                float(t["reverse_net"]),
        }

    return {
        "date": t["date"],
        "signal_ts": t["signal_ts"],
        "symbol": t["symbol"],
        "original_direction":
            t["direction"],
        "executed_direction":
            t["direction"],
        "normal_or_reverse":
            "NORMAL",
        "gross":
            float(t["normal_gross"]),
        "costs":
            float(t["normal_costs"]),
        "net":
            float(t["normal_net"]),
    }


def make_fixed_strategy(name):
    rows = []

    for _, t in df.iterrows():

        side = str(t["direction"]).upper()

        if name == "CURRENT_NORMAL":
            use_reverse = False

        elif name == "ALL_REVERSE":
            use_reverse = True

        elif name == "REVERSE_BUY_ONLY":
            # Original BUY -> SELL.
            # Original SELL remains SELL.
            use_reverse = (
                side == "BUY"
            )

        elif name == "REVERSE_SELL_ONLY":
            # Original SELL -> BUY.
            # Original BUY remains BUY.
            use_reverse = (
                side == "SELL"
            )

        else:
            raise ValueError(name)

        r = row_from_side(
            t, use_reverse
        )
        r["strategy"] = name
        rows.append(r)

    return pd.DataFrame(rows)


# --------------------------------------------------
# FIXED BENCHMARKS
# --------------------------------------------------

fixed_names = [
    "CURRENT_NORMAL",
    "ALL_REVERSE",
    "REVERSE_BUY_ONLY",
    "REVERSE_SELL_ONLY",
]

fixed_frames = {
    name: make_fixed_strategy(name)
    for name in fixed_names
}

summary_rows = []

for name, x in fixed_frames.items():
    summary_rows.append({
        "strategy": name,
        "use_di": None,
        "adx_min": None,
        "buy_clv_min": None,
        "sell_clv_max": None,
        "volume_min": None,
        **metrics(x)
    })


# --------------------------------------------------
# REGIME SWEEP
# --------------------------------------------------

trade_outputs = []

for use_di in [False, True]:

    for adx_min in ADX_LEVELS:

        for buy_clv_min in BUY_CLV_LEVELS:

            # Same symmetry used in previous experiment
            sell_clv_max = (
                1.0 - buy_clv_min
            )

            for volume_min in VOLUME_LEVELS:

                directional_clv = (
                    (
                        (df["direction"] == "BUY")
                        &
                        (
                            df["clv01"]
                            >= buy_clv_min
                        )
                    )
                    |
                    (
                        (df["direction"] == "SELL")
                        &
                        (
                            df["clv01"]
                            <= sell_clv_max
                        )
                    )
                )

                quality = (
                    (df["adx"] >= adx_min)
                    &
                    (
                        df["volume_ratio"]
                        >= volume_min
                    )
                    &
                    directional_clv
                )

                if use_di:
                    di_ok = (
                        (
                            (df["direction"] == "BUY")
                            &
                            (
                                df["plus_di"]
                                >
                                df["minus_di"]
                            )
                        )
                        |
                        (
                            (df["direction"] == "SELL")
                            &
                            (
                                df["minus_di"]
                                >
                                df["plus_di"]
                            )
                        )
                    )

                    quality = quality & di_ok

                for strategy in [
                    "PASS_NORMAL_FAIL_SKIP",
                    "PASS_NORMAL_FAIL_REVERSE",
                    "FAIL_REVERSE_ONLY",
                ]:

                    rows = []

                    for idx, t in df.iterrows():

                        passed = bool(
                            quality.loc[idx]
                        )

                        execute = None

                        if (
                            strategy
                            ==
                            "PASS_NORMAL_FAIL_SKIP"
                        ):
                            if passed:
                                execute = "NORMAL"

                        elif (
                            strategy
                            ==
                            "PASS_NORMAL_FAIL_REVERSE"
                        ):
                            execute = (
                                "NORMAL"
                                if passed
                                else "REVERSE"
                            )

                        elif (
                            strategy
                            ==
                            "FAIL_REVERSE_ONLY"
                        ):
                            if not passed:
                                execute = "REVERSE"

                        if execute is None:
                            continue

                        r = row_from_side(
                            t,
                            use_reverse=(
                                execute == "REVERSE"
                            )
                        )

                        r.update({
                            "strategy":
                                strategy,
                            "filter_pass":
                                passed,
                            "use_di":
                                use_di,
                            "adx_min":
                                adx_min,
                            "buy_clv_min":
                                buy_clv_min,
                            "sell_clv_max":
                                sell_clv_max,
                            "volume_min":
                                volume_min,
                        })

                        rows.append(r)

                    result = pd.DataFrame(rows)

                    m = metrics(result)

                    normal_rows = (
                        result[
                            result[
                                "normal_or_reverse"
                            ] == "NORMAL"
                        ]
                        if not result.empty
                        else pd.DataFrame()
                    )

                    reverse_rows = (
                        result[
                            result[
                                "normal_or_reverse"
                            ] == "REVERSE"
                        ]
                        if not result.empty
                        else pd.DataFrame()
                    )

                    mn = metrics(normal_rows)
                    mr = metrics(reverse_rows)

                    summary_rows.append({
                        "strategy": strategy,
                        "use_di": use_di,
                        "adx_min": adx_min,
                        "buy_clv_min":
                            buy_clv_min,
                        "sell_clv_max":
                            sell_clv_max,
                        "volume_min":
                            volume_min,
                        **m,
                        "normal_trades":
                            mn["trades"],
                        "normal_net":
                            mn["net"],
                        "reverse_trades":
                            mr["trades"],
                        "reverse_net":
                            mr["net"],
                    })

                    if not result.empty:
                        trade_outputs.append(
                            result
                        )


summary = pd.DataFrame(
    summary_rows
)

summary["robustness_score"] = (
    summary["net"]
    +
    summary[
        "profit_factor"
    ].replace(
        np.inf, 5
    ).fillna(0) * 25
    +
    summary[
        "win_rate_pct"
    ] * 0.25
    +
    summary[
        "profitable_days"
    ] * 10
    -
    summary[
        "losing_days"
    ] * 5
    +
    summary[
        "max_drawdown"
    ] * 0.50
)

summary.loc[
    summary["trades"] < 20,
    "robustness_score"
] -= 100


# --------------------------------------------------
# SAVE
# --------------------------------------------------

summary.to_csv(
    OUT / "strategy_sweep.csv",
    index=False
)

if trade_outputs:
    pd.concat(
        trade_outputs,
        ignore_index=True
    ).to_csv(
        OUT / "trade_level.csv",
        index=False
    )


# --------------------------------------------------
# PRINT HELPERS
# --------------------------------------------------

def pretty(frame):
    if frame.empty:
        print("None")
        return

    cols = [
        c for c in [
            "strategy",
            "use_di",
            "adx_min",
            "buy_clv_min",
            "volume_min",
            "trades",
            "wins",
            "losses",
            "win_rate_pct",
            "gross",
            "costs",
            "net",
            "avg_net",
            "profit_factor",
            "max_drawdown",
            "profitable_days",
            "losing_days",
            "normal_trades",
            "normal_net",
            "reverse_trades",
            "reverse_net",
            "robustness_score",
        ]
        if c in frame.columns
    ]

    print(
        frame[cols].to_string(
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
                "normal_net":
                    lambda v:
                        f"Rs {v:,.2f}",
                "reverse_net":
                    lambda v:
                        f"Rs {v:,.2f}",
                "max_drawdown":
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
                "robustness_score":
                    lambda v:
                        f"{v:.2f}",
            }
        )
    )


print(
    "\n===== FIXED DIRECTION BENCHMARKS ====="
)

fixed = summary[
    summary["strategy"].isin(
        fixed_names
    )
].copy()

pretty(fixed)


print(
    "\n===== BEST PASS NORMAL / FAIL SKIP ====="
)

x = summary[
    summary["strategy"]
    ==
    "PASS_NORMAL_FAIL_SKIP"
].sort_values(
    "robustness_score",
    ascending=False
).head(10)

pretty(x)


print(
    "\n===== BEST PASS NORMAL / FAIL REVERSE ====="
)

x = summary[
    summary["strategy"]
    ==
    "PASS_NORMAL_FAIL_REVERSE"
].sort_values(
    "robustness_score",
    ascending=False
).head(10)

pretty(x)


print(
    "\n===== BEST FAIL REVERSE ONLY ====="
)

x = summary[
    summary["strategy"]
    ==
    "FAIL_REVERSE_ONLY"
].sort_values(
    "robustness_score",
    ascending=False
).head(10)

pretty(x)


print(
    "\n===== TOP 20 OVERALL BY NET ====="
)

pretty(
    summary.sort_values(
        "net",
        ascending=False
    ).head(20)
)


print(
    "\n===== TOP 20 OVERALL BY ROBUSTNESS ====="
)

pretty(
    summary.sort_values(
        "robustness_score",
        ascending=False
    ).head(20)
)


# --------------------------------------------------
# BEST CONFIG DAYWISE
# --------------------------------------------------

best = (
    summary[
        summary["strategy"]
        ==
        "PASS_NORMAL_FAIL_REVERSE"
    ]
    .sort_values(
        "robustness_score",
        ascending=False
    )
    .iloc[0]
)

mask = (
    (df["adx"] >= best["adx_min"])
    &
    (df["volume_ratio"] >= best["volume_min"])
    &
    (
        (
            (df["direction"] == "BUY")
            &
            (
                df["clv01"]
                >= best["buy_clv_min"]
            )
        )
        |
        (
            (df["direction"] == "SELL")
            &
            (
                df["clv01"]
                <= best["sell_clv_max"]
            )
        )
    )
)

if bool(best["use_di"]):
    mask &= (
        (
            (df["direction"] == "BUY")
            &
            (
                df["plus_di"]
                >
                df["minus_di"]
            )
        )
        |
        (
            (df["direction"] == "SELL")
            &
            (
                df["minus_di"]
                >
                df["plus_di"]
            )
        )
    )

best_rows = []

for idx, t in df.iterrows():
    passed = bool(mask.loc[idx])

    r = row_from_side(
        t,
        use_reverse=not passed
    )

    r["filter_pass"] = passed

    best_rows.append(r)

best_trades = pd.DataFrame(
    best_rows
)

day = (
    best_trades.groupby("date")
    .agg(
        trades=("symbol", "size"),
        normal_trades=(
            "normal_or_reverse",
            lambda s:
                int((s == "NORMAL").sum())
        ),
        reverse_trades=(
            "normal_or_reverse",
            lambda s:
                int((s == "REVERSE").sum())
        ),
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
        gross=("gross", "sum"),
        costs=("costs", "sum"),
        net=("net", "sum"),
    )
    .reset_index()
)

day["cumulative_net"] = (
    day["net"].cumsum()
)

day.to_csv(
    OUT / "best_regime_daywise.csv",
    index=False
)

print(
    "\n===== BEST REGIME CONFIGURATION ====="
)

print(
    "ADX >=",
    best["adx_min"]
)

print(
    "BUY CLV >=",
    best["buy_clv_min"]
)

print(
    "SELL CLV <=",
    best["sell_clv_max"]
)

print(
    "Volume >=",
    best["volume_min"]
)

print(
    "DI:",
    bool(best["use_di"])
)

print(
    "\n===== BEST REGIME DAYWISE ====="
)

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

print(
    "\nWrote:",
    OUT
)
