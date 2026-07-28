"""
Replays EVERY signal today (across the full watchlist) using the same
fetch pattern as the LIVE bot (5-day lookback for proper EMA warm-up,
filtered to today's date only), sized at CAPITAL=100000 with the real
margin-based cap applied -- giving the complete "what if I'd started
today with Rs1,00,000" picture, not just the 5 trades that actually
executed at Rs500.
"""
import time
import pandas as pd
import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles
from indicators import add_indicators
from strategy import evaluate
from costs import net_pnl_for_trade

kite = get_kite_client()

instruments_nse = kite.instruments('NSE')
token_map = {row['tradingsymbol']: row['instrument_token'] for row in instruments_nse}

SIM_CAPITAL = 100000
budget = SIM_CAPITAL * cfg.MAX_POSITION_SIZE_PCT / 100
risk_amount = SIM_CAPITAL * cfg.RISK_PER_TRADE_PCT / 100

all_signals = []
print(f"Scanning {len(cfg.WATCHLIST)} symbols for today's REAL signals (simulating Rs{SIM_CAPITAL:,} capital)...")

for idx, w in enumerate(cfg.WATCHLIST):
    symbol, exchange = w['symbol'], w['exchange']
    token = token_map.get(symbol)
    if token is None:
        continue
    try:
        full_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
        time.sleep(0.2)
        full_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
        time.sleep(0.2)
        if full_15m.empty or full_5m.empty:
            continue
        full_15m, full_5m = add_indicators(full_15m, full_5m, cfg)
    except Exception:
        continue

    today_str = pd.Timestamp.now(tz=full_5m['date'].dt.tz).strftime('%Y-%m-%d')

    for i in range(len(full_5m)):
        row_date_str = full_5m.iloc[i]['date'].strftime('%Y-%m-%d')
        if row_date_str != today_str:
            continue
        df_15m_slice = full_15m[full_15m['date'] <= full_5m.iloc[i]['date']]
        df_5m_slice = full_5m.iloc[:i+1]
        try:
            signal = evaluate(symbol, df_15m_slice, df_5m_slice, cfg)
        except Exception:
            continue
        if signal:
            all_signals.append({'symbol': symbol, 'exchange': exchange, 'signal': signal})

    if (idx + 1) % 20 == 0:
        print(f"  ...scanned {idx+1}/{len(cfg.WATCHLIST)}")

print()
print(f"Found {len(all_signals)} total signal(s) today across the whole watchlist.")
print()

total_gross = 0
total_net = 0

for entry in all_signals:
    symbol, exchange, signal = entry['symbol'], entry['exchange'], entry['signal']
    direction = signal.direction
    entry_price, stop = signal.entry_price, signal.stop_loss
    per_share_risk = abs(entry_price - stop)
    if per_share_risk <= 0:
        continue
    qty_risk_based = int(risk_amount / per_share_risk)
    if qty_risk_based <= 0:
        continue

    try:
        transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_params = [{
            "exchange": exchange, "tradingsymbol": symbol,
            "transaction_type": transaction_type, "variety": cfg.VARIETY,
            "product": cfg.PRODUCT, "order_type": cfg.ORDER_TYPE_ENTRY,
            "quantity": 1, "price": 0, "trigger_price": 0,
        }]
        margin_per_share = kite.order_margins(order_params)[0].get("total", 0)
        time.sleep(0.2)
    except Exception:
        margin_per_share = 0

    if margin_per_share > 0:
        max_qty_by_margin = int(budget / margin_per_share)
        final_qty = min(qty_risk_based, max_qty_by_margin)
    else:
        final_qty = qty_risk_based

    if final_qty <= 0:
        print(f"{symbol:12s} {direction:4s} entry={entry_price:8.2f} -> qty=0 after margin cap, still skipped")
        continue

    # NOTE: this uses the SIGNAL's entry/stop -- we don't have a real
    # "exit" for signals that never executed at Rs500, so we can only
    # report what quantity/risk WOULD have been taken, not a real P&L
    # outcome, unless this symbol is one of today's 5 that actually closed.
    print(f"{symbol:12s} {direction:4s} entry={entry_price:8.2f} stop={stop:8.2f} "
          f"qty(risk)={qty_risk_based:5d} final_qty={final_qty:5d}")
