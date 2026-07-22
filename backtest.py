"""
Simple bar-by-bar backtester.

Walks forward through 5-min candles, replaying only the trend/entry
data that would have been "known" at that point in time (no
lookahead), applies the same strategy + risk logic as main.py, and
reports trade-by-trade results.

Usage:
    python backtest.py RELIANCE 2026-06-01 2026-07-01
    python backtest.py RELIANCE 2026-06-01 2026-07-01 BSE
"""

import sys

import pandas as pd

import config as cfg
from auth import get_kite_client
from data_feed import get_instrument_token
from indicators import add_indicators
from strategy import evaluate
from risk_manager import RiskManager
from costs import net_pnl_for_trade


def fetch_range(kite, token, interval, from_date, to_date):
    data = kite.historical_data(token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]


def run_backtest_data(symbol: str, from_date: str, to_date: str, exchange: str = "NSE"):
    """
    Runs the backtest and returns a results dict instead of printing —
    used by both the CLI entry point below and the web dashboard's
    comparison tool. Uses whatever cfg.USE_ADX_FILTER etc. are set to
    at call time, so callers can flip settings before calling this to
    compare scenarios.
    """
    kite = get_kite_client()
    token = get_instrument_token(kite, symbol, exchange)

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
                cost_result = net_pnl_for_trade(position["direction"], position["qty"], position["entry"], row["close"])
                risk.record_trade_result(cost_result["net_pnl"])  # kill-switch tracks REAL (post-cost) P&L
                trades.append({
                    **position, "exit": row["close"], "exit_time": row["date"],
                    "result": "target" if hit_target else "stop",
                    "gross_pnl": cost_result["gross_pnl"],
                    "costs": cost_result["costs"],
                    "pnl": cost_result["net_pnl"],  # "pnl" = net, for backwards compatibility with anything reading this field
                })
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
        return {"symbol": symbol, "from_date": from_date, "to_date": to_date,
                "total_trades": 0, "win_rate": None,
                "total_pnl": 0.0, "avg_pnl": None,
                "total_gross_pnl": 0.0, "total_costs": 0.0,
                "trades": []}

    wins = trades_df[trades_df["pnl"] > 0]
    return {
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
        "total_trades": len(trades_df),
        "win_rate": len(wins) / len(trades_df) * 100,
        "total_pnl": trades_df["pnl"].sum(),              # NET (after estimated costs) -- what actually matters
        "avg_pnl": trades_df["pnl"].mean(),
        "total_gross_pnl": trades_df["gross_pnl"].sum(),   # before costs, for comparison
        "total_costs": trades_df["costs"].sum(),
        "trades": trades_df.to_dict("records"),
    }


def run_backtest(symbol: str, from_date: str, to_date: str, exchange: str = "NSE"):
    """CLI wrapper — runs the backtest and prints a human-readable summary."""
    result = run_backtest_data(symbol, from_date, to_date, exchange)

    if result["total_trades"] == 0:
        print("No trades generated in this period.")
        return result

    print(f"\n--- Backtest results for {exchange}:{symbol}: {from_date} to {to_date} ---")
    print(f"Total trades: {result['total_trades']}")
    print(f"Win rate: {result['win_rate']:.1f}%")
    print(f"Gross P&L (before costs): {result['total_gross_pnl']:.2f}")
    print(f"Estimated trading costs:  -{result['total_costs']:.2f}")
    print(f"Net P&L (after costs):    {result['total_pnl']:.2f}")
    print(f"Avg NET P&L per trade: {result['avg_pnl']:.2f}")
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("Usage: python backtest.py SYMBOL FROM_DATE TO_DATE [EXCHANGE]")
        sys.exit(1)
    exch = sys.argv[4] if len(sys.argv) == 5 else "NSE"
    run_backtest(sys.argv[1], sys.argv[2], sys.argv[3], exch)
