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
import time

import pandas as pd

import config as cfg
from auth import get_kite_client
from data_feed import get_instrument_token
from indicators import add_indicators
from strategy import evaluate
from risk_manager import RiskManager
from costs import net_pnl_for_trade


def fetch_range(kite, token, interval, from_date, to_date, max_retries=3):
    last_exc = None
    for attempt in range(max_retries):
        try:
            data = kite.historical_data(token, from_date, to_date, interval)
            df = pd.DataFrame(data)
            if df.empty:
                return df
            df["date"] = pd.to_datetime(df["date"])
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            last_exc = e
            time.sleep(2 ** attempt)  # 1s, 2s, 4s
    print(f"fetch_range: giving up after {max_retries} attempts: {last_exc}")
    return pd.DataFrame()


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

    risk = RiskManager(cfg, persist=False)  # never touch live day_state.json
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
    losses = trades_df[trades_df["pnl"] < 0]

    gross_profit = wins["pnl"].sum() if not wins.empty else 0.0
    gross_loss = abs(losses["pnl"].sum()) if not losses.empty else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    avg_winner = wins["pnl"].mean() if not wins.empty else None
    avg_loser = losses["pnl"].mean() if not losses.empty else None

    win_rate_frac = len(wins) / len(trades_df)
    expectancy = (win_rate_frac * (avg_winner or 0)) + ((1 - win_rate_frac) * (avg_loser or 0))

    # Max drawdown on the cumulative equity curve (in currency, not %)
    equity_curve = trades_df["pnl"].cumsum()
    running_max = equity_curve.cummax()
    drawdown = equity_curve - running_max
    max_drawdown = drawdown.min() if not drawdown.empty else 0.0

    return {
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
        "total_trades": len(trades_df),
        "win_rate": win_rate_frac * 100,
        "total_pnl": trades_df["pnl"].sum(),              # NET (after estimated costs) -- what actually matters
        "avg_pnl": trades_df["pnl"].mean(),
        "total_gross_pnl": trades_df["gross_pnl"].sum(),   # before costs, for comparison
        "total_costs": trades_df["costs"].sum(),
        "profit_factor": profit_factor,                   # gross profit / gross loss; None if no losses
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "expectancy": expectancy,                          # expected net P&L per trade
        "max_drawdown": max_drawdown,                       # most negative point of cumulative P&L (currency)
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
    pf = result['profit_factor']
    print(f"Profit factor: {pf:.2f}" if pf is not None else "Profit factor: N/A (no losing trades)")
    aw = result['avg_winner']
    al = result['avg_loser']
    print(f"Avg winner: {aw:.2f}" if aw is not None else "Avg winner: N/A")
    print(f"Avg loser: {al:.2f}" if al is not None else "Avg loser: N/A")
    print(f"Expectancy per trade: {result['expectancy']:.2f}")
    print(f"Max drawdown: {result['max_drawdown']:.2f}")
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("Usage: python backtest.py SYMBOL FROM_DATE TO_DATE [EXCHANGE]")
        sys.exit(1)
    exch = sys.argv[4] if len(sys.argv) == 5 else "NSE"
    run_backtest(sys.argv[1], sys.argv[2], sys.argv[3], exch)
