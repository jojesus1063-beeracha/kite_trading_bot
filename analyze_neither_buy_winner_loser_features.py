from pathlib import Path
import pandas as pd
import numpy as np

SRC = Path(
    "runtime/proposed_logic_broker_sl_replay/"
    "trade_level.csv"
)

OUT = Path(
    "runtime/neither_buy_feature_analysis"
)
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(SRC)

# Replayable only
if "status" in df.columns:
    x = df[
        df["status"].astype(str).str.upper() == "OK"
    ].copy()
else:
    x = df.copy()

# Normalize booleans
def to_bool(v):
    return str(v).strip().lower() in {
        "true", "1", "yes", "y"
    }

for c in ["breakout", "pullback"]:
    x[c] = x[c].map(to_bool)

x["direction"] = (
    x["direction"]
    .astype(str)
    .str.upper()
)

x["sim_net"] = pd.to_numeric(
    x["sim_net"],
    errors="coerce"
)

x = x.dropna(subset=["sim_net"])

# Target group:
# BUY and neither breakout nor pullback
nb = x[
    (x["direction"] == "BUY")
    &
    (~x["breakout"])
    &
    (~x["pullback"])
].copy()

nb["winner"] = nb["sim_net"] > 0

print("===== TARGET GROUP =====")
print("Trades :", len(nb))
print("Wins   :", int(nb["winner"].sum()))
print("Losses :", int((~nb["winner"]).sum()))
print(
    "Net P&L:",
    f"Rs {nb['sim_net'].sum():,.2f}"
)

# --------------------------------------------------
# Candidate numeric features
# --------------------------------------------------

FEATURES = [
    "adx14",
    "ema_distance_atr",
    "volume_ratio20",
    "atr_multiple",
    "clv",
    "atr14",
    "expected_gross_proxy",
    "confirmation_count",
    "close",
    "ema9",
    "ema21",
    "vwap",
    "body",
    "lower_wick",
    "upper_wick",
]

# Normalize available numeric columns
available = []

for c in FEATURES:
    if c in nb.columns:
        nb[c] = pd.to_numeric(
            nb[c],
            errors="coerce"
        )
        available.append(c)

print("\nFeatures available:")
print(available)

# --------------------------------------------------
# Winner vs loser descriptive stats
# --------------------------------------------------

rows = []

for c in available:

    w = nb.loc[
        nb["winner"],
        c
    ].dropna()

    l = nb.loc[
        ~nb["winner"],
        c
    ].dropna()

    if len(w) < 3 or len(l) < 3:
        continue

    row = {
        "feature": c,

        "winner_n": len(w),
        "loser_n": len(l),

        "winner_mean": w.mean(),
        "loser_mean": l.mean(),

        "winner_median": w.median(),
        "loser_median": l.median(),

        "winner_p25": w.quantile(.25),
        "winner_p75": w.quantile(.75),

        "loser_p25": l.quantile(.25),
        "loser_p75": l.quantile(.75),

        "winner_min": w.min(),
        "winner_max": w.max(),

        "loser_min": l.min(),
        "loser_max": l.max(),
    }

    pooled = pd.concat([w, l])

    std = pooled.std()

    if pd.notna(std) and std > 0:
        row["mean_diff_std"] = (
            w.mean() - l.mean()
        ) / std
    else:
        row["mean_diff_std"] = np.nan

    rows.append(row)

stats = pd.DataFrame(rows)

stats["abs_separation"] = (
    stats["mean_diff_std"].abs()
)

stats = stats.sort_values(
    "abs_separation",
    ascending=False
)

print(
    "\n===== WINNER VS LOSER FEATURE SEPARATION ====="
)

print(
    stats[
        [
            "feature",
            "winner_n",
            "loser_n",
            "winner_mean",
            "loser_mean",
            "winner_median",
            "loser_median",
            "winner_p25",
            "winner_p75",
            "loser_p25",
            "loser_p75",
            "mean_diff_std",
        ]
    ].to_string(
        index=False,
        float_format=lambda v: f"{v:.4f}"
    )
)

# --------------------------------------------------
# Boolean/categorical feature tests
# --------------------------------------------------

BOOL_FEATURES = [
    "direction_pass",
    "adx_pass",
    "vwap_pass",
    "ema_distance_pass",
    "structure_pass",
    "volume_pass",
    "atr_pass",
    "clv_pass",
    "confirmation_pass",
    "independent_pass",
    "cost_pass",
    "rejection",
    "near_ema",
    "near_vwap",
    "resumption",
    "wick_reject",
]

bool_rows = []

for c in BOOL_FEATURES:

    if c not in nb.columns:
        continue

    vals = nb[c].map(to_bool)

    for value in [True, False]:

        mask = vals == value

        g = nb[mask]

        if g.empty:
            continue

        wins = int(g["winner"].sum())
        losses = len(g) - wins

        bool_rows.append({
            "feature": c,
            "value": value,
            "trades": len(g),
            "wins": wins,
            "losses": losses,
            "win_rate_pct":
                wins / len(g) * 100,
            "net_pnl":
                g["sim_net"].sum(),
            "avg_pnl":
                g["sim_net"].mean(),
        })

bool_stats = pd.DataFrame(bool_rows)

print(
    "\n===== BOOLEAN FEATURE PERFORMANCE ====="
)

if not bool_stats.empty:
    print(
        bool_stats.sort_values(
            ["feature", "value"],
            ascending=[True, False]
        ).to_string(
            index=False,
            formatters={
                "win_rate_pct":
                    lambda v: f"{v:.1f}%",
                "net_pnl":
                    lambda v: f"Rs {v:,.2f}",
                "avg_pnl":
                    lambda v: f"Rs {v:,.2f}",
            }
        )
    )

# --------------------------------------------------
# Threshold search for numeric features
# --------------------------------------------------

threshold_rows = []

for c in available:

    vals = nb[c].dropna()

    if len(vals) < 20:
        continue

    # use quantiles to avoid overfitting every raw value
    qs = np.linspace(
        0.10,
        0.90,
        17
    )

    thresholds = sorted(
        set(
            float(vals.quantile(q))
            for q in qs
        )
    )

    for t in thresholds:

        for op in ["LE", "GE"]:

            if op == "LE":
                g = nb[
                    nb[c].notna()
                    &
                    (nb[c] <= t)
                ]
            else:
                g = nb[
                    nb[c].notna()
                    &
                    (nb[c] >= t)
                ]

            if len(g) < 10:
                continue

            wins = int(
                g["winner"].sum()
            )

            losses = (
                len(g) - wins
            )

            net = g[
                "sim_net"
            ].sum()

            threshold_rows.append({
                "feature": c,
                "operator": op,
                "threshold": t,
                "trades": len(g),
                "wins": wins,
                "losses": losses,
                "win_rate_pct":
                    wins / len(g) * 100,
                "net_pnl": net,
                "avg_pnl":
                    net / len(g),
                "winner_capture_pct":
                    wins
                    /
                    max(
                        1,
                        int(
                            nb["winner"].sum()
                        )
                    )
                    * 100,
                "loser_allow_pct":
                    losses
                    /
                    max(
                        1,
                        int(
                            (~nb["winner"]).sum()
                        )
                    )
                    * 100,
            })

thr = pd.DataFrame(
    threshold_rows
)

if not thr.empty:

    # Reward net + win rate, penalize tiny samples.
    thr["score"] = (
        thr["net_pnl"]
        +
        thr["win_rate_pct"] * 0.5
        +
        thr["winner_capture_pct"] * 0.25
        -
        thr["loser_allow_pct"] * 0.25
    )

    print(
        "\n===== BEST SINGLE-FEATURE THRESHOLDS ====="
    )

    print(
        thr.sort_values(
            "score",
            ascending=False
        )[
            [
                "feature",
                "operator",
                "threshold",
                "trades",
                "wins",
                "losses",
                "win_rate_pct",
                "winner_capture_pct",
                "loser_allow_pct",
                "net_pnl",
                "avg_pnl",
                "score",
            ]
        ]
        .head(40)
        .to_string(
            index=False,
            formatters={
                "threshold":
                    lambda v: f"{v:.4f}",
                "win_rate_pct":
                    lambda v: f"{v:.1f}%",
                "winner_capture_pct":
                    lambda v: f"{v:.1f}%",
                "loser_allow_pct":
                    lambda v: f"{v:.1f}%",
                "net_pnl":
                    lambda v: f"Rs {v:,.2f}",
                "avg_pnl":
                    lambda v: f"Rs {v:,.2f}",
                "score":
                    lambda v: f"{v:.2f}",
            }
        )
    )

# --------------------------------------------------
# Pairwise threshold search from top features
# --------------------------------------------------

top_features = (
    stats[
        stats["feature"].isin(
            available
        )
    ]
    .head(6)["feature"]
    .tolist()
)

pair_rows = []

for i, f1 in enumerate(
    top_features
):

    for f2 in top_features[
        i+1:
    ]:

        vals1 = nb[f1].dropna()
        vals2 = nb[f2].dropna()

        if (
            len(vals1) < 20
            or len(vals2) < 20
        ):
            continue

        t1s = [
            vals1.quantile(.25),
            vals1.quantile(.50),
            vals1.quantile(.75),
        ]

        t2s = [
            vals2.quantile(.25),
            vals2.quantile(.50),
            vals2.quantile(.75),
        ]

        for t1 in t1s:
            for t2 in t2s:
                for op1 in ["LE", "GE"]:
                    for op2 in ["LE", "GE"]:

                        m1 = (
                            nb[f1] <= t1
                            if op1 == "LE"
                            else
                            nb[f1] >= t1
                        )

                        m2 = (
                            nb[f2] <= t2
                            if op2 == "LE"
                            else
                            nb[f2] >= t2
                        )

                        g = nb[
                            m1 & m2
                        ].copy()

                        if len(g) < 8:
                            continue

                        wins = int(
                            g["winner"].sum()
                        )

                        losses = (
                            len(g)-wins
                        )

                        net = (
                            g["sim_net"].sum()
                        )

                        pair_rows.append({
                            "feature1": f1,
                            "op1": op1,
                            "threshold1": t1,

                            "feature2": f2,
                            "op2": op2,
                            "threshold2": t2,

                            "trades": len(g),
                            "wins": wins,
                            "losses": losses,

                            "win_rate_pct":
                                wins
                                /
                                len(g)
                                * 100,

                            "net_pnl":
                                net,

                            "avg_pnl":
                                net
                                /
                                len(g),

                            "winner_capture_pct":
                                wins
                                /
                                max(
                                    1,
                                    int(
                                        nb[
                                            "winner"
                                        ].sum()
                                    )
                                )
                                * 100,

                            "loser_allow_pct":
                                losses
                                /
                                max(
                                    1,
                                    int(
                                        (
                                            ~nb[
                                                "winner"
                                            ]
                                        ).sum()
                                    )
                                )
                                * 100,
                        })

pairs = pd.DataFrame(
    pair_rows
)

if not pairs.empty:

    pairs["score"] = (
        pairs["net_pnl"]
        +
        pairs[
            "win_rate_pct"
        ] * 0.5
        +
        pairs[
            "winner_capture_pct"
        ] * 0.25
        -
        pairs[
            "loser_allow_pct"
        ] * 0.25
    )

    print(
        "\n===== BEST TWO-FEATURE COMBINATIONS ====="
    )

    print(
        pairs.sort_values(
            "score",
            ascending=False
        )[
            [
                "feature1",
                "op1",
                "threshold1",
                "feature2",
                "op2",
                "threshold2",
                "trades",
                "wins",
                "losses",
                "win_rate_pct",
                "winner_capture_pct",
                "loser_allow_pct",
                "net_pnl",
                "avg_pnl",
                "score",
            ]
        ]
        .head(40)
        .to_string(
            index=False,
            formatters={
                "threshold1":
                    lambda v:
                        f"{v:.4f}",
                "threshold2":
                    lambda v:
                        f"{v:.4f}",
                "win_rate_pct":
                    lambda v:
                        f"{v:.1f}%",
                "winner_capture_pct":
                    lambda v:
                        f"{v:.1f}%",
                "loser_allow_pct":
                    lambda v:
                        f"{v:.1f}%",
                "net_pnl":
                    lambda v:
                        f"Rs {v:,.2f}",
                "avg_pnl":
                    lambda v:
                        f"Rs {v:,.2f}",
                "score":
                    lambda v:
                        f"{v:.2f}",
            }
        )
    )

# --------------------------------------------------
# SAVE
# --------------------------------------------------

nb.to_csv(
    OUT / "trade_level.csv",
    index=False
)

stats.to_csv(
    OUT / "feature_separation.csv",
    index=False
)

if not bool_stats.empty:
    bool_stats.to_csv(
        OUT / "boolean_feature_performance.csv",
        index=False
    )

if not thr.empty:
    thr.to_csv(
        OUT / "single_feature_thresholds.csv",
        index=False
    )

if not pairs.empty:
    pairs.to_csv(
        OUT / "two_feature_combinations.csv",
        index=False
    )

print(
    "\nWrote:",
    OUT
)
