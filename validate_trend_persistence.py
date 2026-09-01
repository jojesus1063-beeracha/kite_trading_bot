"""
Retrospective validation of a trend-persistence pre-filter concept.

Computes, for each of Friday's 30 shortlisted symbols, the % of 15m
bars classified UP or DOWN (not indeterminate) by the REAL get_trend()
function over the 5 trading days BEFORE 2026-08-07 -- using data
already covered by the existing 25-day lookback fetch.

This does NOT change the watchlist, the entry pipeline, or any
production behavior. It only answers one question: would this score,
computed from data available BEFORE Friday even started, have
correctly flagged TATACHEM/SBFC as low-value and SWIGGY as high-value
-- i.e. is "trend persistence" a genuinely predictive signal, or did
Friday's bimodal pattern just not exist yet the days before?

Read-only. Never writes state, never places orders.

Run:
    BOT_DIR=~/kite_trading_bot python3 validate_trend_persistence.py
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
PRIOR_WINDOW_DAYS = 5  # trading days strictly before TARGET_DATE used to compute the persistence score


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


def _classify_bars(df15, cfg, dates_included):
    counts = Counter()
    subset = df15[df15["date"].dt.date.isin(dates_included)]
    subset = subset[subset["date"].apply(_within_entry_window)]
    for _, row in subset.iterrows():
        trend = get_trend(row, cfg)
        counts[trend if trend is not None else "INDETERMINATE"] += 1
    return counts


def main():
    kite = get_kite_client()
    all_rows = _watchlist_rows()
    symbols = [s for s, _ in all_rows]
    exchange_map = dict(all_rows)
    shortlisted, _, _ = select_scan_universe(
        symbols, [], getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30)
    )

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    print("=" * 100)
    print("RETROSPECTIVE VALIDATION: trend-persistence score (prior days) vs Friday's actual result")
    print("=" * 100)
    print(f"{'Symbol':<14} {'PriorScore':>11} {'PriorBars':>10}  |  {'FridayIndet%':>13} {'FridayBars':>11}  |  {'Predicted?'}")
    print("-" * 100)

    results = []

    for symbol in shortlisted:
        exchange = exchange_map.get(symbol, "NSE")
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                     from_date=start, to_date=end, trim_incomplete=False)
            if df15_raw.empty:
                continue
            df15, _ = add_indicators(df15_raw, df15_raw.copy(), cfg)

            all_dates = sorted(df15[df15["date"].dt.date < TARGET_DATE.date()]["date"].dt.date.unique())
            prior_dates = set(all_dates[-PRIOR_WINDOW_DAYS:]) if len(all_dates) >= PRIOR_WINDOW_DAYS else set(all_dates)
            friday_dates = {TARGET_DATE.date()}

            prior_counts = _classify_bars(df15, cfg, prior_dates)
            friday_counts = _classify_bars(df15, cfg, friday_dates)

            prior_total = sum(prior_counts.values())
            friday_total = sum(friday_counts.values())
            if prior_total == 0 or friday_total == 0:
                continue

            prior_persistence_pct = (prior_total - prior_counts.get("INDETERMINATE", 0)) / prior_total * 100
            friday_indet_pct = friday_counts.get("INDETERMINATE", 0) / friday_total * 100

            predicted_good = prior_persistence_pct >= 50
            actually_good = friday_indet_pct < 50
            hit = predicted_good == actually_good

            results.append({
                "symbol": symbol, "prior_persistence_pct": prior_persistence_pct,
                "prior_total": prior_total, "friday_indet_pct": friday_indet_pct,
                "friday_total": friday_total, "hit": hit,
            })

            print(f"{symbol:<14} {prior_persistence_pct:>10.1f}% {prior_total:>10d}  |  "
                  f"{friday_indet_pct:>12.1f}% {friday_total:>11d}  |  {'YES' if hit else 'no'}")

        except Exception as exc:
            print(f"{symbol:<14} ERROR: {exc}")

    hits = sum(1 for r in results if r["hit"])
    total = len(results)
    print("-" * 100)
    print(f"\nPrediction accuracy: {hits}/{total} ({hits/total*100:.1f}%)" if total else "\nNo results.")
    print("(A prior-days persistence score >=50% predicting Friday's indeterminate rate <50%, or the reverse)")

    print("\nSpecific cases requested for validation:")
    for target_symbol in ("TATACHEM", "SBFC", "SWIGGY"):
        match = next((r for r in results if r["symbol"] == target_symbol), None)
        if match:
            print(f"  {target_symbol}: prior_persistence={match['prior_persistence_pct']:.1f}% "
                  f"-> Friday_indeterminate={match['friday_indet_pct']:.1f}% "
                  f"({'correctly predicted' if match['hit'] else 'NOT predicted'})")
        else:
            print(f"  {target_symbol}: insufficient data to evaluate")


if __name__ == "__main__":
    main()
