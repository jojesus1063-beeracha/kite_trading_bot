"""
De-duplicated trend-regime diagnostic for 2026-08-07.

The earlier replay's 69% TREND_OR_ADX figure counts every 5-minute
candle-evaluation independently, but the trend itself is derived from
the 15-minute timeframe -- meaning the same 15m classification gets
counted up to 3 times over (once per 5-min sub-candle within it). This
script instead classifies each DISTINCT 15-minute bar exactly once,
using the real get_trend() function, across all 30 shortlisted
symbols, to get the genuine regime breakdown the earlier percentage
was likely overstating.

Read-only. Never writes state, never places orders.

Run:
    BOT_DIR=~/kite_trading_bot python3 trend_regime_20260807.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BOT_DIR = Path(os.path.expanduser(os.environ.get("BOT_DIR", "~/kite_trading_bot"))).resolve()
if not BOT_DIR.exists():
    raise SystemExit(f"BOT_DIR does not exist: {BOT_DIR}")
sys.path.insert(0, str(BOT_DIR))

import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token
from indicators import add_indicators
from strategy import get_trend
from scan_latency import select_scan_universe

TARGET_DATE = pd.Timestamp("2026-08-07")
LOOKBACK_DAYS = 25


def _watchlist_rows():
    rows = []
    for item in cfg.WATCHLIST:
        if isinstance(item, str):
            rows.append((item, "NSE"))
        else:
            rows.append((item["symbol"], item.get("exchange", "NSE")))
    return rows


def _within_entry_window(ts):
    t = pd.Timestamp(ts).time()
    start = datetime.strptime(cfg.NO_ENTRY_BEFORE, "%H:%M").time()
    end = datetime.strptime(cfg.NO_ENTRY_AFTER, "%H:%M").time()
    return start <= t <= end


def main():
    kite = get_kite_client()
    all_rows = _watchlist_rows()
    symbols = [s for s, _ in all_rows]
    exchange_map = dict(all_rows)
    shortlisted, _, _ = select_scan_universe(
        symbols, [], getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30)
    )

    print("=" * 90)
    print("DE-DUPLICATED 15-MINUTE TREND-REGIME BREAKDOWN -- 2026-08-07")
    print("=" * 90)
    print(f"Shortlist: {len(shortlisted)} symbols")
    print(f"ADX_MODE={getattr(cfg, 'ADX_MODE', None)} "
          f"USE_ADX_FILTER={getattr(cfg, 'USE_ADX_FILTER', None)} "
          f"ADX_THRESHOLD={getattr(cfg, 'ADX_THRESHOLD', None)}")
    print()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    overall = Counter()
    per_symbol_rows = []

    for idx, symbol in enumerate(shortlisted, 1):
        exchange = exchange_map.get(symbol, "NSE")
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                     from_date=start, to_date=end, trim_incomplete=False)
            if df15_raw.empty:
                continue
            df15, _ = add_indicators(df15_raw, df15_raw.copy(), cfg)
            today15 = df15[df15["date"].dt.date == TARGET_DATE.date()]
            today15 = today15[today15["date"].apply(_within_entry_window)]

            sym_counts = Counter()
            for _, row in today15.iterrows():
                trend = get_trend(row, cfg)  # default require_vwap=True, matching the real production gate
                label = trend if trend is not None else "INDETERMINATE"
                sym_counts[label] += 1
                overall[label] += 1

            total = sum(sym_counts.values())
            if total > 0:
                per_symbol_rows.append({
                    "symbol": symbol, "bars": total,
                    "UP": sym_counts.get("UP", 0), "DOWN": sym_counts.get("DOWN", 0),
                    "INDETERMINATE": sym_counts.get("INDETERMINATE", 0),
                })
            print(f"[{idx:02d}/{len(shortlisted)}] {symbol:<14} "
                  f"UP={sym_counts.get('UP', 0):2d} DOWN={sym_counts.get('DOWN', 0):2d} "
                  f"INDETERMINATE={sym_counts.get('INDETERMINATE', 0):2d} (of {total} 15m bars)")

        except Exception as exc:
            print(f"[{idx:02d}/{len(shortlisted)}] {symbol:<14} ERROR: {exc}")

    total_bars = sum(overall.values())
    print("\n" + "=" * 90)
    print("AGGREGATE (each 15-minute bar counted exactly once per symbol)")
    print("=" * 90)
    print(f"Total distinct symbol-bars evaluated: {total_bars}")
    for label in ("UP", "DOWN", "INDETERMINATE"):
        count = overall.get(label, 0)
        pct = (count / total_bars * 100) if total_bars else 0
        print(f"  {label:<15} {count:5d}  ({pct:.1f}%)")

    print(f"\nFor comparison, the earlier per-5-min-candle-evaluation figure was "
          f"69% TREND_OR_ADX rejection (1,416 of 2,039). This aggregate is the "
          f"de-duplicated equivalent -- each 15-minute bar's classification "
          f"counted once, not repeated per 5-minute sub-candle within it.")


if __name__ == "__main__":
    main()
