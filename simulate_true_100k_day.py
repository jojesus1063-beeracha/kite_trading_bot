"""
The definitive "what if I'd started today with Rs1,00,000" simulation.

Unlike a per-symbol backtest, this:
  - Uses ONE shared RiskManager across the whole watchlist (matching
    main.py's real behavior -- a halt on one symbol's loss stops NEW
    entries everywhere, not just that symbol)
  - Merges all symbols' 5-min candles into a single chronological
    timeline and walks forward through TIME, not symbol-by-symbol
  - Applies the real margin-based cap (cap_quantity_by_margin) on
    every sized trade, using live order_margins() calls
  - Uses real cost-inclusive P&L (net_pnl_for_trade), same as the
    live bot and the tested backtest engine
  - Fetches extra lookback days so EMA(50) is properly warmed up,
    then only acts on candles from TODAY
"""
import time
import pandas as pd
from datetime import date, timedelta
import config as cfg
from auth import get_kite_client
from data_feed import get_instrument_token, fetch_candles
from indicators import add_indicators
from strategy import evaluate
from risk_manager import RiskManager
from costs import net_pnl_for_trade
from executor import cap_quantity_by_margin

SIM_CAPITAL = 100000
today_str = date.today().strftime("%Y-%m-%d")

original_capital = cfg.CAPITAL
cfg.CAPITAL = SIM_CAPITAL

kite = get_kite_client()

print(f"Fetching data for {len(cfg.WATCHLIST)} symbols (this will take a few minutes)...")
symbol_data = {}
for idx, w in enumerate(cfg.WATCHLIST):
    symbol, exchange = w['symbol'], w['exchange']
    try:
        token = get_instrument_token(kite, symbol, exchange)
        full_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=7)
        time.sleep(0.2)
        full_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=7)
        time.sleep(0.2)
        if full_15m.empty or full_5m.empty:
            continue
        full_15m, full_5m = add_indicators(full_15m, full_5m, cfg)
        today_5m = full_5m[full_5m['date'].dt.strftime('%Y-%m-%d') == today_str].copy()
        if today_5m.empty:
            continue
        symbol_data[symbol] = {"exchange": exchange, "full_15m": full_15m, "full_5m": full_5m, "today_5m": today_5m}
    except Exception:
        continue
    if (idx + 1) % 20 == 0:
        print(f"  ...fetched {idx+1}/{len(cfg.WATCHLIST)}")

print(f"Got today's data for {len(symbol_data)} symbols. Building merged timeline...")

# Merge every symbol's today-candles into one (timestamp, symbol) timeline
timeline = []
for symbol, d in symbol_data.items():
    for _, row in d["today_5m"].iterrows():
        timeline.append((row["date"], symbol))
timeline.sort(key=lambda x: x[0])

risk = RiskManager(cfg, persist=False)  # ONE shared risk tracker, like main.py
open_positions = {}  # symbol -> position dict
trades = []
skipped_for_margin = []

print(f"Walking forward through {len(timeline)} candle-events chronologically...")

for ts, symbol in timeline:
    d = symbol_data[symbol]
    row = d["today_5m"][d["today_5m"]["date"] == ts].iloc[0]

    # --- manage existing position in this symbol ---
    if symbol in open_positions:
        pos = open_positions[symbol]
        hit_stop = (row["close"] <= pos["stop"]) if pos["direction"] == "BUY" else (row["close"] >= pos["stop"])
        hit_target = (row["close"] >= pos["target"]) if pos["direction"] == "BUY" else (row["close"] <= pos["target"])
        if hit_stop or hit_target:
            cost_result = net_pnl_for_trade(pos["direction"], pos["qty"], pos["entry"], row["close"])
            risk.record_trade_result(cost_result["net_pnl"])
            trades.append({**pos, "symbol": symbol, "exit": row["close"], "exit_time": ts,
                            "result": "target" if hit_target else "stop", **cost_result})
            del open_positions[symbol]
        continue

    # --- look for a new entry (only if not halted / under position cap) ---
    if not risk.can_take_new_trade(current_open_count=len(open_positions)):
        continue

    df_15m_slice = d["full_15m"][d["full_15m"]["date"] <= ts]
    df_5m_slice = d["full_5m"][d["full_5m"]["date"] <= ts]
    try:
        signal = evaluate(symbol, df_15m_slice, df_5m_slice, cfg)
    except Exception:
        continue
    if not signal:
        continue

    qty = risk.position_size(signal.entry_price, signal.stop_loss)
    if qty <= 0:
        continue

    try:
        capped_qty = cap_quantity_by_margin(kite, symbol, signal.direction, qty, d["exchange"], cfg)
        time.sleep(0.15)
    except Exception:
        capped_qty = qty

    if capped_qty <= 0:
        skipped_for_margin.append(symbol)
        continue

    open_positions[symbol] = {
        "direction": signal.direction, "qty": capped_qty,
        "entry": signal.entry_price, "stop": signal.stop_loss,
        "target": signal.target, "entry_time": ts,
    }

cfg.CAPITAL = original_capital  # restore immediately

print()
print("=" * 70)
print(f"Still-open positions at end of scan (would be force square-off'd): {list(open_positions.keys())}")
print(f"Signals skipped entirely due to margin cap -> qty=0: {skipped_for_margin}")
print()

if not trades:
    print("No completed trades in this simulation.")
else:
    df = pd.DataFrame(trades)
    print(df[["symbol", "direction", "qty", "entry", "exit", "result", "gross_pnl", "costs", "net_pnl"]].to_string(index=False))
    print()
    print(f"Total trades: {len(df)}")
    print(f"Wins: {(df['net_pnl'] > 0).sum()}  Losses: {(df['net_pnl'] < 0).sum()}")
    print(f"Total gross P&L: Rs{df['gross_pnl'].sum():+.2f}")
    print(f"Total costs: Rs{df['costs'].sum():.2f}")
    print(f"Total NET P&L: Rs{df['net_pnl'].sum():+.2f}")
    print(f"Final halted status: {risk.day.halted} ({risk.day.halt_reason if risk.day.halted else 'not halted'})")
