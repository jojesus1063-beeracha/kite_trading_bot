#!/usr/bin/env python3

import csv
import re
from collections import defaultdict
from pathlib import Path

import config
from costs import net_pnl_for_trade

FILE = Path("runtime/ema_threshold_replay/trades.csv")

CAPITAL = float(config.CAPITAL)
EMA_LIMIT = 2.00
RISK_PCT = 0.40
DAILY_LOSS_PCT = 0.50
LEVERAGE = 4.0
MAX_TRADES_PER_DAY = 7

START_TIMES = (
    "09:15",
    "09:20",
    "09:30",
    "09:45",
    "10:00",
    "10:30",
)

TIME_RE = re.compile(r"(\d{2}:\d{2})(?::\d{2})?")


def extract_time(value):
    matches = TIME_RE.findall(str(value or ""))
    return matches[0] if matches else None


def load_rows():
    rows = []

    with FILE.open() as handle:
        for row in csv.DictReader(handle):
            if abs(
                float(row["ema_limit"]) - EMA_LIMIT
            ) > 0.0001:
                continue

            row["entry"] = float(row["entry"])
            row["exit"] = float(row["exit"])
            row["stop_loss"] = float(row["stop_loss"])
            row["clock_time"] = extract_time(
                row.get("timestamp") or row.get("time")
            )

            if not row["clock_time"]:
                continue

            rows.append(row)

    return sorted(
        rows,
        key=lambda row: (
            row["date"],
            row["clock_time"],
            row.get("symbol", ""),
        ),
    )


def simulate(all_rows, start_time):
    eligible = [
        row for row in all_rows
        if row["clock_time"] >= start_time
    ]


    by_day = defaultdict(list)
    for row in eligible:
        by_day[row["date"]].append(row)

    trades = []
    equity = 0.0
    peak = 0.0
    drawdown = 0.0

    for date in sorted(by_day):
        day_net = 0.0
        day_trades = 0

        for row in by_day[date]:
            if day_trades >= MAX_TRADES_PER_DAY:
                break

            if day_net <= -(
                CAPITAL * DAILY_LOSS_PCT / 100
            ):
                break

            entry = row["entry"]
            stop = row["stop_loss"]
            risk_per_share = abs(entry - stop)

            if risk_per_share <= 0:
                continue

            risk_amount = CAPITAL * RISK_PCT / 100
            risk_qty = int(risk_amount / risk_per_share)
            margin_qty = int(
                CAPITAL * LEVERAGE / entry
            )
            quantity = min(risk_qty, margin_qty)

            if quantity <= 0:
                continue

            result = net_pnl_for_trade(
                row["direction"],
                quantity,
                entry,
                row["exit"],
            )

            gross = float(result["gross_pnl"])
            costs = float(result["costs"])
            net = float(result["net_pnl"])

            trades.append({
                "net": net,
                "gross": gross,
                "costs": costs,
            })

            day_net += net
            day_trades += 1
            equity += net
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)

    wins = sum(t["net"] > 0 for t in trades)
    net = sum(t["net"] for t in trades)

    return {
        "opportunities": len(eligible),
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "gross": sum(t["gross"] for t in trades),
        "costs": sum(t["costs"] for t in trades),
        "net": net,
        "drawdown": drawdown,
    }


rows = load_rows()

print(f"Capital: ₹{CAPITAL:,.2f}")
print(f"Risk per trade: {RISK_PCT}%")
print(f"EMA maximum: {EMA_LIMIT} ATR")
print(f"Available EMA-2.0 records: {len(rows)}")
print()
print(
    "Start   Opp  Trades  Wins  Losses"
    "   Gross     Costs      Net        DD"
)

for start in START_TIMES:
    r = simulate(rows, start)
    print(
        f"{start:>5}"
        f" {r['opportunities']:>5}"
        f" {r['trades']:>7}"
        f" {r['wins']:>5}"
        f" {r['losses']:>7}"
        f" ₹{r['gross']:>8.2f}"
        f" ₹{r['costs']:>8.2f}"
        f" ₹{r['net']:>8.2f}"
        f" ₹{r['drawdown']:>9.2f}"
    )
