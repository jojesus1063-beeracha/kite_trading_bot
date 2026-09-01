"""
Shortlist expansion comparison: ENTRY_SCAN_SHORTLIST_SIZE 30 vs 60.

Runs the Aug-7 replay twice in a single pass -- once with the current
production shortlist size, once with the expanded size -- and reports
the full before/after comparison.

CRITICAL: does NOT modify config.py, user_config.json, or any
production setting. The shortlist size is passed directly to
select_scan_universe() as a local variable. strategy.evaluate() and
every downstream gate run completely unmodified in both passes.

Read-only. Never writes state, never places orders.
"""

from __future__ import annotations

import os
import sys
import time
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
import strategy as strategy_mod
import rvol as rvol_mod
import watchlist_filters as watchlist_filters_mod
from scan_latency import select_scan_universe
from market_trend import NIFTY50_TOKEN

CAPTURED = []

def _capturing_mark(symbol, status, detail=None, **kwargs):
    CAPTURED.append({"symbol": symbol, "status": status, "detail": detail or {}})
    return None

strategy_mod.mark_filter_status = _capturing_mark
rvol_mod.mark_filter_status = _capturing_mark
watchlist_filters_mod.mark_filter_status = _capturing_mark

TARGET_DATE = pd.Timestamp("2026-08-07")
LOOKBACK_DAYS = 25

OLD_SIZE = int(getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30))
NEW_SIZE = 60


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


def run_pass(kite, shortlist, exchange_map, nifty_15m, cache, label):
    print(f"\n{'=' * 90}\nPASS: {label} -- {len(shortlist)} symbols\n{'=' * 90}", flush=True)
    raw_signals, gate_passed = [], []
    rejections = Counter()
    fetch_count = 0
    t0 = time.time()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    for idx, symbol in enumerate(shortlist, 1):
        exchange = exchange_map.get(symbol, "NSE")
        try:
            if symbol in cache:
                df15, df5 = cache[symbol]
            else:
                token = get_instrument_token(kite, symbol, exchange)
                df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                         from_date=start, to_date=end, trim_incomplete=False)
                df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                        from_date=start, to_date=end, trim_incomplete=False)
                fetch_count += 2
                if df15_raw.empty or df5_raw.empty:
                    rejections["NO_DATA"] += 1
                    continue
                df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
                cache[symbol] = (df15, df5)

            today5 = df5[df5["date"].dt.date == TARGET_DATE.date()]
            for row_index in today5.index:
                candle_ts = df5.loc[row_index, "date"]
                if not _within_entry_window(candle_ts):
                    continue
                five_slice = df5.loc[:row_index].copy()
                fifteen_slice = df15[df15["date"] <= candle_ts].copy()
                index_slice = nifty_15m[nifty_15m["date"] <= candle_ts].copy()
                if fifteen_slice.empty:
                    continue

                mark_before = len(CAPTURED)
                sig = strategy_mod.evaluate(symbol, fifteen_slice, five_slice, index_slice, cfg)
                if sig is None:
                    new_marks = CAPTURED[mark_before:]
                    rejections[new_marks[-1]["status"] if new_marks else "UNKNOWN"] += 1
                    continue

                raw_signals.append({
                    "symbol": symbol, "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp), "entry": float(sig.entry_price),
                    "stop": float(sig.stop_loss), "target": float(sig.target),
                    "confidence": sig.confidence,
                })

                eligibility, _ = watchlist_filters_mod.classify_direction_eligibility(fifteen_slice, cfg)
                if eligibility not in (watchlist_filters_mod.NOT_ENABLED, sig.direction):
                    rejections["EMA200_DIRECTIONAL"] += 1
                    continue
                rvol_ok, rvol_value, _ = rvol_mod.passes_rvol_threshold(five_slice, cfg)
                if not rvol_ok:
                    rejections["RVOL_GATE"] += 1
                    continue

                gate_passed.append({
                    "symbol": symbol, "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp), "entry": float(sig.entry_price),
                    "stop": float(sig.stop_loss), "target": float(sig.target),
                    "confidence": sig.confidence,
                    "rvol": None if rvol_value is None else float(rvol_value),
                })
        except Exception as exc:
            rejections["ERROR"] += 1
            print(f"  ERROR {symbol}: {exc}")

    return {
        "label": label, "shortlist": shortlist, "raw": raw_signals,
        "gate_passed": gate_passed, "rejections": rejections,
        "duration": time.time() - t0, "fetches": fetch_count,
    }


def main():
    kite = get_kite_client()
    all_rows = _watchlist_rows()
    symbols = [s for s, _ in all_rows]
    exchange_map = dict(all_rows)

    short_old, _, _ = select_scan_universe(symbols, [], OLD_SIZE)
    short_new, _, _ = select_scan_universe(symbols, [], NEW_SIZE)

    print("=" * 90)
    print("SHORTLIST EXPANSION COMPARISON -- 2026-08-07 -- NO CONFIG MODIFIED")
    print("=" * 90)
    print(f"Watchlist (auto_watchlist.py output): {len(symbols)}")
    print(f"Production ENTRY_SCAN_SHORTLIST_SIZE: {OLD_SIZE}")
    print(f"Comparison size:                      {NEW_SIZE}")
    print(f"PAPER_TRADING (unchanged):            {getattr(cfg, 'PAPER_TRADING', None)}")

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()
    nifty_raw = fetch_candles(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME,
                               from_date=start, to_date=end, trim_incomplete=False)
    if nifty_raw.empty:
        raise SystemExit("FATAL: could not fetch NIFTY 50 data.")
    nifty_15m, _ = add_indicators(nifty_raw, nifty_raw.copy(), cfg)

    cache = {}
    old = run_pass(kite, short_old, exchange_map, nifty_15m, cache, f"OLD ({OLD_SIZE})")
    new = run_pass(kite, short_new, exchange_map, nifty_15m, cache, f"NEW ({NEW_SIZE})")

    added = [s for s in short_new if s not in short_old]

    print("\n" + "=" * 90)
    print("COMPARISON")
    print("=" * 90)
    print(f"{'':<28}{'OLD ' + str(OLD_SIZE):>12}{'NEW ' + str(NEW_SIZE):>12}")
    print("-" * 90)
    print(f"{'Watchlist':<28}{len(symbols):>12}{len(symbols):>12}")
    print(f"{'Evaluated':<28}{len(short_old):>12}{len(short_new):>12}")
    print(f"{'Excluded':<28}{len(symbols)-len(short_old):>12}{len(symbols)-len(short_new):>12}")
    print(f"{'Raw signals':<28}{len(old['raw']):>12}{len(new['raw']):>12}")
    print(f"{'Gate-passed signals':<28}{len(old['gate_passed']):>12}{len(new['gate_passed']):>12}")
    for key in ("TREND_OR_ADX", "PULLBACK_SEQUENCE", "TIME_FILTER", "MACRO_INDEX_FILTER",
                "VWAP_ACCEPTANCE", "EMA200_CONFIRMATION", "ENTRY_TIMING",
                "EMA200_DIRECTIONAL", "RVOL_GATE"):
        print(f"{key:<28}{old['rejections'].get(key,0):>12}{new['rejections'].get(key,0):>12}")
    print("-" * 90)
    print(f"{'Scan duration (s)':<28}{old['duration']:>12.1f}{new['duration']:>12.1f}")
    print(f"{'Candle fetches':<28}{old['fetches']:>12}{new['fetches']:>12}"
          "   (NEW reuses cache from OLD; real cost = 2 per symbol)")
    print(f"\nReal-world fetch cost per scan: OLD={len(short_old)*2}  NEW={len(short_new)*2}")

    print(f"\nADDITIONAL SYMBOLS EVALUATED ({len(added)}):")
    print("  " + ", ".join(added) if added else "  (none)")

    old_raw_keys = {(r["symbol"], r["timestamp"]) for r in old["raw"]}
    old_gate_keys = {(r["symbol"], r["timestamp"]) for r in old["gate_passed"]}
    new_raw_only = [r for r in new["raw"] if (r["symbol"], r["timestamp"]) not in old_raw_keys]
    new_gate_only = [r for r in new["gate_passed"] if (r["symbol"], r["timestamp"]) not in old_gate_keys]

    print(f"\nADDITIONAL RAW SIGNALS: {len(new_raw_only)}")
    for r in sorted(new_raw_only, key=lambda x: x["timestamp"]):
        print(f"  {r['timestamp']:%H:%M}  {r['symbol']:<14} {r['direction']:<4} "
              f"entry={r['entry']:.2f} stop={r['stop']:.2f} target={r['target']:.2f} "
              f"confidence={r['confidence']}")

    print(f"\nADDITIONAL GATE-PASSED SIGNALS: {len(new_gate_only)}")
    for r in sorted(new_gate_only, key=lambda x: x["timestamp"]):
        print(f"  {r['timestamp']:%H:%M}  {r['symbol']:<14} {r['direction']:<4} "
              f"entry={r['entry']:.2f} stop={r['stop']:.2f} target={r['target']:.2f} "
              f"rvol={r['rvol']} confidence={r['confidence']}")

    print("\n" + "=" * 90)
    print("BACKWARD COMPATIBILITY CHECK")
    print("=" * 90)
    preserved = all((r["symbol"], r["timestamp"]) in
                    {(x["symbol"], x["timestamp"]) for x in new["raw"]} for r in old["raw"])
    print(f"Every OLD signal still present in NEW: {'PASS' if preserved else 'FAIL'}")
    print("(Expanding the shortlist must never remove an existing signal.)")


if __name__ == "__main__":
    main()
