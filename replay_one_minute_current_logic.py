#!/usr/bin/env python3

import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

import config as cfg
import candle_eligibility
import entry_quality
import paper_contrarian_launcher as launcher
import strategy
from costs import net_pnl_for_trade

FROM_DATE = "2026-08-04"
TO_DATE = "2026-08-19"

CAPITAL = 5000.0
RISK_PCT = 0.40
LEVERAGE = 4.0
MAX_TRADES_PER_DAY = 7
MAX_DAILY_LOSS_PCT = 0.50

OUTPUT = Path(
    "runtime/one_minute_replay"
)
OUTPUT.mkdir(parents=True, exist_ok=True)

# One-minute API interval. The leading 1 lets the strategy
# correctly understand that each candle is one minute.
cfg.ENTRY_TIMEFRAME = "1minute"

# Preserve the original three-minute time horizons.
launcher.EMA_FAST = 27
launcher.EMA_SLOW = 63
cfg.ENTRY_EMA = 27
cfg.VOLUME_LOOKBACK = 60
cfg.BREAKOUT_LOOKBACK = 60
cfg.BREAKOUT_VOLUME_PERIOD = 60
cfg.BREAKOUT_ATR_PERIOD = 42
cfg.PAPER_CANDLE_VOLUME_LOOKBACK = 60
cfg.PAPER_COST_MOVE_LOOKBACK = 42

# Current live controls.
cfg.CAPITAL = CAPITAL
cfg.RISK_PER_TRADE_PCT = RISK_PCT
cfg.MAX_OPEN_POSITIONS = 1
cfg.MAX_TRADES_PER_DAY = MAX_TRADES_PER_DAY
cfg.MAX_DAILY_LOSS_PCT = MAX_DAILY_LOSS_PCT
cfg.MAX_POSITION_SIZE_PCT = 100.0

# Install the combined strategy in non-ordering replay mode.
cfg.PAPER_TRADING = True
launcher.install_two_indicator_patch(
    live_combined=False
)

# Re-enable the live-only independent confirmation rule.
cfg.PAPER_REQUIRE_INDEPENDENT_CONFIRMATION = True
cfg.PAPER_MAX_TRADES_PER_SYMBOL = 999
cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = 0

# Keep replay audit separate from live/paper audit.
launcher.AUDIT_DIR = OUTPUT / "audit"
launcher.ENTRY_AUDIT = (
    launcher.AUDIT_DIR / "entry_audit.jsonl"
)
launcher.CONFIG_AUDIT = (
    launcher.AUDIT_DIR / "session_config.json"
)

# Historical candles are completed by definition. Avoid comparing
# their timestamps with today's wall-clock time.
candle_eligibility._fresh_completed_candle = (
    lambda timestamp, cfg_obj, now=None: (
        True,
        {
            "historical_replay": True,
            "timestamp": str(timestamp),
        },
    )
)

# Entry-quality ATR14 on a three-minute chart represented
# approximately 42 minutes; use ATR42 on one-minute candles.
_original_atr = entry_quality._atr

def replay_atr(candles, period=42):
    return _original_atr(
        candles,
        period=42,
    )

entry_quality._atr = replay_atr

# Live EMA-distance maximum.
entry_quality.MAX_EMA_DISTANCE_ATR = 2.00
entry_quality.MAX_SIGNAL_BODY_ATR = 1.50
entry_quality.MAX_VWAP_DISTANCE_ATR = 2.50

# Add entry-quality as a hard gate to the patched strategy.
_base_evaluate = strategy.evaluate

def evaluate_with_quality(
    symbol,
    df_15m,
    df_entry,
    df_index_15m,
    cfg_obj,
):
    signal = _base_evaluate(
        symbol,
        df_15m,
        df_entry,
        df_index_15m,
        cfg_obj,
    )

    if signal is None:
        return None

    quality = entry_quality.assess_entry_quality(
        signal,
        df_entry,
    )

    return signal if quality.accepted else None

strategy.evaluate = evaluate_with_quality

# Import after installing the strategy patch.
import backtest

# backtest.py imported evaluate directly, so replace its reference.
backtest.evaluate = evaluate_with_quality

# Kite calls the one-minute interval "minute".
_original_fetch = backtest.fetch_range

def rate_limited_fetch(
    kite,
    token,
    interval,
    from_date,
    to_date,
    max_retries=3,
):
    time.sleep(0.45)

    kite_interval = (
        "minute"
        if interval == "1minute"
        else interval
    )

    return _original_fetch(
        kite,
        token,
        kite_interval,
        from_date,
        to_date,
        max_retries=max_retries,
    )

backtest.fetch_range = rate_limited_fetch

all_trades = []

print("ONE-MINUTE HISTORICAL REPLAY")
print("----------------------------")
print("Period:", FROM_DATE, "to", TO_DATE)
print("Watchlist symbols:", len(cfg.WATCHLIST))

for number, item in enumerate(
    cfg.WATCHLIST,
    start=1,
):
    symbol = item["symbol"]
    exchange = item.get("exchange", "NSE")

    print(
        f"[{number}/{len(cfg.WATCHLIST)}] "
        f"{exchange}:{symbol}"
    )

    try:
        result = backtest.run_backtest_data(
            symbol,
            FROM_DATE,
            TO_DATE,
            exchange,
        )
    except Exception as exc:
        print("  ERROR:", exc)
        continue

    for trade in result.get("trades", []):
        trade["symbol"] = symbol
        trade["exchange"] = exchange
        all_trades.append(trade)

all_trades.sort(
    key=lambda row: pd.Timestamp(
        row["entry_time"]
    )
)

# Reapply current portfolio controls across all symbols.
selected = []
open_until = None
daily = defaultdict(
    lambda: {
        "trades": 0,
        "net": 0.0,
    }
)

for trade in all_trades:
    entry_time = pd.Timestamp(
        trade["entry_time"]
    )
    exit_time = pd.Timestamp(
        trade["exit_time"]
    )
    day = str(entry_time.date())

    state = daily[day]

    if state["trades"] >= MAX_TRADES_PER_DAY:
        continue

    if state["net"] <= -(
        CAPITAL
        * MAX_DAILY_LOSS_PCT
        / 100
    ):
        continue

    if (
        open_until is not None
        and entry_time < open_until
    ):
        continue

    entry = float(trade["entry"])
    stop = float(trade["stop"])
    exit_price = float(trade["exit"])

    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        continue

    risk_amount = (
        CAPITAL * RISK_PCT / 100
    )
    quantity_by_risk = int(
        risk_amount / risk_per_share
    )
    quantity_by_margin = int(
        CAPITAL * LEVERAGE / entry
    )
    quantity = min(
        quantity_by_risk,
        quantity_by_margin,
    )

    if quantity <= 0:
        continue

    result = net_pnl_for_trade(
        trade["direction"],
        quantity,
        entry,
        exit_price,
    )

    net = float(result["net_pnl"])

    selected.append({
        **trade,
        "qty_current": quantity,
        "gross_current": float(
            result["gross_pnl"]
        ),
        "costs_current": float(
            result["costs"]
        ),
        "net_current": net,
    })

    state["trades"] += 1
    state["net"] += net
    open_until = exit_time

wins = sum(
    row["net_current"] > 0
    for row in selected
)
losses = sum(
    row["net_current"] < 0
    for row in selected
)
gross = sum(
    row["gross_current"]
    for row in selected
)
costs = sum(
    row["costs_current"]
    for row in selected
)
net = sum(
    row["net_current"]
    for row in selected
)

equity = 0.0
peak = 0.0
max_drawdown = 0.0

for row in selected:
    equity += row["net_current"]
    peak = max(peak, equity)
    max_drawdown = min(
        max_drawdown,
        equity - peak,
    )

print()
print("RESULT")
print("------")
print("Generated raw trades :", len(all_trades))
print("Portfolio trades     :", len(selected))
print("Wins                 :", wins)
print("Losses               :", losses)
print(
    "Win rate             :",
    (
        f"{wins / len(selected) * 100:.2f}%"
        if selected else "N/A"
    ),
)
print(f"Gross P&L            : ₹{gross:.2f}")
print(f"Costs                : ₹{costs:.2f}")
print(f"Net P&L              : ₹{net:.2f}")
print(
    f"Maximum drawdown     : "
    f"₹{max_drawdown:.2f}"
)

print()
print("TRADES")
print("------")

for row in selected:
    print(
        row["entry_time"],
        row["symbol"],
        row["direction"],
        "qty=",
        row["qty_current"],
        "entry=",
        round(float(row["entry"]), 2),
        "exit=",
        round(float(row["exit"]), 2),
        "result=",
        row["result"],
        "net=₹%.2f" % row["net_current"],
    )
