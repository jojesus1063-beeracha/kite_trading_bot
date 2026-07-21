"""
Simple bar-by-bar backtester.

Walks forward through 5-min candles, replaying only the trend/entry
data that would have been "known" at that point in time (no
lookahead), applies the same strategy + risk logic as main.py, and
reports trade-by-trade results.

Usage:
    python backtest.py RELIANCE 2026-06-01 2026-07-01
"""

import sys

import pandas as pd

import config as cfg
from auth import get_kite_client
from data_feed import get_instrument_token
from indicators import add_indicators
from strategy import evaluate
from risk_manager import RiskManager


def fetch_range(kite, token, interval, from_date, to_date):
    data = kite.historical_data(token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]


def run_backtest(symbol: str, from_date: str, to_date: str):
    kite = get_kite_client()
    token = get_instrument_token(kite, symbol, cfg.EXCHANGE)

    full_15m = fetch_range(kite, token, cfg.TREND_TIMEFRAME, from_date, to_date)
    full_5m = fetch_range(kite, token, cfg.ENTRY_TIMEFRAME, from_date, to_date)
    full_15m, full_5m = add_indicators(full_15m, full_5m, cfg)

    risk = RiskManager(cfg)
    trades = []
    position = None

    for i in range(len(full_5m)):
        row = full_5m.iloc[i]

        if position is not None:
            hit_stop = (row["close"] <= position["stop"]) if position["direction"] == "BUY" else (row["close"] >= position["stop"])
            hit_target = (row["close"] >= position["target"]) if position["direction"] == "BUY" else (row["close"] <= position["target"])
            if hit_stop or hit_target:
                pnl_per_share = (row["close"] - position["entry"]) if position["direction"] == "BUY" else (position["entry"] - row["close"])
                pnl = pnl_per_share * position["qty"]
                risk.record_trade_result(pnl)
                trades.append({**position, "exit": row["close"], "exit_time": row["date"],
                               "result": "target" if hit_target else "stop", "pnl": pnl})
                position = None
            continue

        if not risk.can_take_new_trade():
            continue

        df_15m_slice = full_15m[full_15m["date"] <= row["date"]]
        df_5m_slice = full_5m.iloc[: i + 1]
        signal = evaluate(symbol, df_15m_slice, df_5m_slice, cfg)
        if signal:
            qty = risk.position_size(signal.entry_price, signal.stop_loss)
            if qty > 0:
                position = {
                    "direction": signal.direction, "qty": qty, "entry": signal.entry_price,
                    "stop": signal.stop_loss, "target": signal.target, "entry_time": row["date"],
                }

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        print("No trades generated in this period.")
        return trades_df

    wins = trades_df[trades_df["pnl"] > 0]
    print(f"\n--- Backtest results for {symbol}: {from_date} to {to_date} ---")
    print(f"Total trades: {len(trades_df)}")
    print(f"Win rate: {len(wins) / len(trades_df) * 100:.1f}%")
    print(f"Total P&L: {trades_df['pnl'].sum():.2f}")
    print(f"Avg P&L per trade: {trades_df['pnl'].mean():.2f}")
    return trades_df


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python backtest.py SYMBOL FROM_DATE TO_DATE")
        sys.exit(1)
    run_backtest(sys.argv[1], sys.argv[2], sys.argv[3])
