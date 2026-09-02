import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0

RPT_PCT = 0.75
MAX_POSITION_PCT = 25.0
MAX_DAILY_LOSS_PCT = 0.50

SL_LEVELS = [0.004, 0.005]

T1_PCT = 0.005
T2_PCT = 0.010

TRADE_FILE = Path(
    "runtime/watchlist_missed_opportunity/"
    "momentum_rvol_matrix/trade_level.csv"
)

MARGIN_FILE = Path(
    "runtime/watchlist_missed_opportunity/"
    "real_money_zone_comparison/"
    "margin_per_share.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "real_money_zone_comparison"
)
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(TRADE_FILE)

for c in [
    "momentum_pct",
    "relative_volume",
    "entry",
    "gross_per_share",
]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

df["signal_ts"] = pd.to_datetime(
    df["date"].astype(str)
    + " "
    + df["signal_time"].astype(str),
    errors="coerce"
)

margin = pd.read_csv(MARGIN_FILE)

margin["margin_per_share"] = pd.to_numeric(
    margin["margin_per_share"],
    errors="coerce"
)

margin_map = dict(
    zip(
        margin["symbol"].astype(str),
        margin["margin_per_share"]
    )
)

def estimate_cost(entry, exit_price, qty):
    # Simple consistent intraday approximation.
    # Replace with your exact cost helper if available.
    turnover = (entry + exit_price) * qty

    brokerage = min(
        20.0,
        entry * qty * 0.0003
    ) + min(
        20.0,
        exit_price * qty * 0.0003
    )

    exchange = turnover * 0.0000345
    sebi = turnover * 0.000001
    stamp = entry * qty * 0.00003
    stt = exit_price * qty * 0.00025
    gst = (brokerage + exchange + sebi) * 0.18

    return (
        brokerage
        + exchange
        + sebi
        + stamp
        + stt
        + gst
    )

def simulate_trade(row, qty, sl_pct):
    side = str(row["direction"]).upper()
    entry = float(row["entry"])
    exit_name = str(row["exit"])

    if side == "BUY":
        stop = entry * (1 - sl_pct)
        t1 = entry * (1 + T1_PCT)
        t2 = entry * (1 + T2_PCT)
    else:
        stop = entry * (1 + sl_pct)
        t1 = entry * (1 - T1_PCT)
        t2 = entry * (1 - T2_PCT)

    # Reconstruct exit price from replay exit label.
    if exit_name == "SL_0.5":
        exit_price = stop

    elif exit_name == "T1_PLUS_T2":
        # 50% at T1 + 50% at T2
        q1 = qty // 2
        q2 = qty - q1

        gross = (
            ((t1-entry) * q1 + (t2-entry) * q2)
            if side == "BUY"
            else ((entry-t1) * q1 + (entry-t2) * q2)
        )

        costs = estimate_cost(entry, t1, q1)
        if q2 > 0:
            costs += estimate_cost(entry, t2, q2)

        return gross, costs, gross-costs, exit_name

    elif exit_name == "T1_PLUS_BE":
        q1 = qty // 2
        q2 = qty - q1

        gross = (
            (t1-entry) * q1
            if side == "BUY"
            else (entry-t1) * q1
        )

        costs = estimate_cost(entry, t1, q1)

        if q2 > 0:
            costs += estimate_cost(
                entry,
                entry,
                q2
            )

        return gross, costs, gross-costs, exit_name

    elif exit_name in (
        "T1_PLUS_EOD",
        "EOD_NO_T1"
    ):
        # Use the replay's per-share gross to preserve
        # the original EOD outcome.
        gps = float(row["gross_per_share"])
        gross = gps * qty

        # approximate effective exit
        if side == "BUY":
            exit_price = entry + gps
        else:
            exit_price = entry - gps

    else:
        gps = float(row["gross_per_share"])
        gross = gps * qty

        if side == "BUY":
            exit_price = entry + gps
        else:
            exit_price = entry - gps

        costs = estimate_cost(
            entry,
            exit_price,
            qty
        )

        return gross, costs, gross-costs, exit_name

    gross = (
        (exit_price-entry) * qty
        if side == "BUY"
        else (entry-exit_price) * qty
    )

    costs = estimate_cost(
        entry,
        exit_price,
        qty
    )

    return gross, costs, gross-costs, exit_name


m = df["momentum_pct"]
r = df["relative_volume"]

ZONES = {
    "MOM_1.00_1.50_RVOL_1.50_2.00":
        (m >= 1.00) & (m < 1.50) &
        (r >= 1.50) & (r < 2.00),

    "MOM_1.00_1.50_RVOL_2.00_3.00":
        (m >= 1.00) & (m < 1.50) &
        (r >= 2.00) & (r < 3.00),

    "MOM_1.00_1.50_RVOL_1.50_3.00":
        (m >= 1.00) & (m < 1.50) &
        (r >= 1.50) & (r < 3.00),
}

all_summary = []
all_trades = []

for zone_name, mask in ZONES.items():

    z = df[mask].copy()

    z = z.sort_values(
        ["date", "signal_ts"]
    )

    for sl_pct in SL_LEVELS:

        daily = defaultdict(
            lambda: {
                "realized": 0.0,
                "trades": 0,
            }
        )

        rows = []

        for _, row in z.iterrows():

            date = str(row["date"])
            symbol = str(row["symbol"])
            entry = float(row["entry"])

            state = daily[date]

            per_share_risk = (
                entry * sl_pct
            )

            risk_budget = (
                CAPITAL
                * RPT_PCT
                / 100.0
            )

            qty_risk = int(
                risk_budget
                / per_share_risk
            )

            mps = margin_map.get(symbol)

            if (
                mps is None
                or not math.isfinite(mps)
                or mps <= 0
            ):
                continue

            margin_budget = (
                AVAILABLE_MARGIN
                * MAX_POSITION_PCT
                / 100.0
            )

            qty_margin = int(
                margin_budget / mps
            )

            qty = min(
                qty_risk,
                qty_margin
            )

            if qty <= 0:
                continue

            proposed_risk = (
                per_share_risk
                * qty
            )

            max_daily_loss = (
                CAPITAL
                * MAX_DAILY_LOSS_PCT
                / 100.0
            )

            realized_loss_used = max(
                0.0,
                -state["realized"]
            )

            if (
                realized_loss_used
                + proposed_risk
                > max_daily_loss
                + 1e-9
            ):
                continue

            gross, costs, net, exit_name = \
                simulate_trade(
                    row,
                    qty,
                    sl_pct
                )

            state["realized"] += net
            state["trades"] += 1

            rows.append({
                "zone": zone_name,
                "sl_pct": sl_pct * 100,
                "date": date,
                "symbol": symbol,
                "momentum_pct":
                    row["momentum_pct"],
                "relative_volume":
                    row["relative_volume"],
                "entry": entry,
                "qty": qty,
                "gross": gross,
                "costs": costs,
                "net": net,
                "exit": exit_name,
            })

        x = pd.DataFrame(rows)

        if x.empty:
            all_summary.append({
                "zone": zone_name,
                "sl_pct": sl_pct*100,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "gross": 0,
                "costs": 0,
                "net": 0,
                "avg_net": 0,
            })
            continue

        wins = int(
            (x["net"] > 0).sum()
        )

        losses = len(x) - wins

        summary = {
            "zone": zone_name,
            "sl_pct": sl_pct*100,
            "trades": len(x),
            "wins": wins,
            "losses": losses,
            "win_rate":
                wins/len(x)*100,
            "gross":
                x["gross"].sum(),
            "costs":
                x["costs"].sum(),
            "net":
                x["net"].sum(),
            "avg_net":
                x["net"].mean(),
            "best_trade":
                x["net"].max(),
            "worst_trade":
                x["net"].min(),
        }

        all_summary.append(summary)
        all_trades.extend(rows)

summary = pd.DataFrame(all_summary)

summary = summary.sort_values(
    "net",
    ascending=False
)

print(
    "\n===== REAL-MONEY WATCHLIST "
    "ZONE COMPARISON ====="
)

print(
    summary.to_string(
        index=False,
        formatters={
            "sl_pct":
                lambda x: f"{x:.1f}%",
            "win_rate":
                lambda x: f"{x:.1f}%",
            "gross":
                lambda x: f"Rs {x:,.2f}",
            "costs":
                lambda x: f"Rs {x:,.2f}",
            "net":
                lambda x: f"Rs {x:,.2f}",
            "avg_net":
                lambda x: f"Rs {x:,.2f}",
            "best_trade":
                lambda x: f"Rs {x:,.2f}",
            "worst_trade":
                lambda x: f"Rs {x:,.2f}",
        }
    )
)

trades = pd.DataFrame(all_trades)

summary.to_csv(
    OUT / "summary.csv",
    index=False
)

trades.to_csv(
    OUT / "trade_level.csv",
    index=False
)

print("\n===== TOP 20 REAL-MONEY TRADES =====")

if not trades.empty:
    print(
        trades.sort_values(
            "net",
            ascending=False
        ).head(20)[
            [
                "zone",
                "sl_pct",
                "date",
                "symbol",
                "momentum_pct",
                "relative_volume",
                "qty",
                "gross",
                "costs",
                "net",
            ]
        ].to_string(
            index=False
        )
    )

print("\nWROTE:", OUT)
