import time
import pandas as pd
import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles
from indicators import add_indicators
from strategy import evaluate

print("Fetching instrument list...")
kite = get_kite_client()
instruments_nse = kite.instruments('NSE')
token_map = {row['tradingsymbol']: row['instrument_token'] for row in instruments_nse}
print(f"Got {len(token_map)} instruments. Starting scan of {len(cfg.WATCHLIST)} watchlist symbols...")

all_signals = []

for idx, w in enumerate(cfg.WATCHLIST):
    symbol, exchange = w['symbol'], w['exchange']
    print(f"[{idx+1}/{len(cfg.WATCHLIST)}] {symbol}...", flush=True)
    token = token_map.get(symbol)
    if token is None:
        print(f"  -> no token found, skipping")
        continue
    try:
        full_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
        time.sleep(0.3)
        full_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
        time.sleep(0.3)
        if full_15m.empty or full_5m.empty:
            print(f"  -> no candle data")
            continue
        full_15m, full_5m = add_indicators(full_15m, full_5m, cfg)
    except Exception as e:
        print(f"  -> error: {e}")
        continue

    # Only evaluate candles from TODAY, not the prior lookback days
    today_str = pd.Timestamp.now(tz=full_5m['date'].dt.tz).strftime('%Y-%m-%d')
    today_5m = full_5m[full_5m['date'].dt.strftime('%Y-%m-%d') == today_str]

    found_here = 0
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
            found_here += 1
            per_share_risk = abs(signal.entry_price - signal.stop_loss)
            all_signals.append({
                'symbol': symbol,
                'time': signal.timestamp,
                'direction': signal.direction,
                'entry': signal.entry_price,
                'stop': signal.stop_loss,
                'per_share_risk': per_share_risk,
            })
    if found_here:
        print(f"  -> {found_here} signal(s) found")

print()
print("=" * 60)
df = pd.DataFrame(all_signals)
if df.empty:
    print('No signals found today at all.')
else:
    df = df.sort_values('per_share_risk', ascending=False)
    print(df.to_string(index=False))
    print()
    print(f'Total signals found today: {len(df)}')
    print()
    max_risk = df['per_share_risk'].max()
    p90_risk = df['per_share_risk'].quantile(0.90)
    median_risk = df['per_share_risk'].median()
    print(f'Capital needed for 100% (every signal gets >=1 share): Rs {max_risk * 100:.0f}')
    print(f'Capital needed for 90% of signals to work: Rs {p90_risk * 100:.0f}')
    print(f'Capital needed for the median/typical signal to work: Rs {median_risk * 100:.0f}')
