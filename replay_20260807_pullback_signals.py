"""Read-only replay of 2026-08-07 signals using the NEW 3-step pullback
strategy, macro NIFTY filter, time filter, and dynamic 2.5x R:R target.

Updated from the original replay_20260807_signals.py (which called the
OLD 4-arg evaluate() signature -- momentum/breakout logic) to match
tonight's pullback rewrite. Same safety guarantees as the original:
- historical-data reads only;
- never imports executor/order functions;
- never writes trade/position/day/filter-diagnostics state;
- replays only the production top-N shortlist order from cfg.WATCHLIST.

Captures the SPECIFIC rejection reason evaluate() already reports
internally via mark_filter_status() (MACRO_INDEX_FILTER,
PULLBACK_SEQUENCE, TIME_FILTER, TREND_OR_ADX, VWAP_ACCEPTANCE,
EMA200_CONFIRMATION, etc.) instead of discarding it, so the actual
blocking behavior of each gate can be reported precisely -- required
to answer "did the macro filter actually block bad trades" honestly,
not just "how many signals total."

Run from any isolated clone with BOT_DIR pointing at the real bot directory:
    BOT_DIR=~/kite_trading_bot python3 replay_20260807_pullback_signals.py
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
import strategy as strategy_mod
import rvol as rvol_mod
import watchlist_filters as watchlist_filters_mod
from scan_latency import select_scan_universe
from market_trend import NIFTY50_TOKEN

# Historical replay must not pollute runtime/filter_diagnostics/latest.json,
# but we DO want to capture what would have been marked, for reporting.
CAPTURED_MARKS = []

def _capturing_mark(symbol, status, detail=None, **kwargs):
    CAPTURED_MARKS.append({"symbol": symbol, "status": status, "detail": detail or {}})
    return None

strategy_mod.mark_filter_status = _capturing_mark
rvol_mod.mark_filter_status = _capturing_mark
watchlist_filters_mod.mark_filter_status = _capturing_mark

TARGET_DATE = pd.Timestamp("2026-08-07")
LOOKBACK_DAYS = 25  # enough warmup for 200x15m EMA and rolling indicators


def _watchlist_rows():
    rows = []
    for item in cfg.WATCHLIST:
        if isinstance(item, str):
            rows.append((item, "NSE"))
        else:
            rows.append((item["symbol"], item.get("exchange", "NSE")))
    return rows


def _same_day(series, target):
    return series.dt.date == target.date()


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
    shortlisted, _, excluded = select_scan_universe(
        symbols, [], getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30)
    )

    print("=" * 88)
    print("2026-08-07 HISTORICAL PULLBACK-STRATEGY SIGNAL REPLAY -- READ ONLY")
    print("=" * 88)
    print(f"BOT_DIR: {BOT_DIR}")
    print(f"Mode in config: {'PAPER' if cfg.PAPER_TRADING else 'LIVE'} (replay itself never places orders)")
    print(f"Watchlist: {len(symbols)} | production shortlist: {len(shortlisted)} | excluded: {len(excluded)}")
    print(f"Entry window: {cfg.NO_ENTRY_BEFORE}-{cfg.NO_ENTRY_AFTER}")
    print(f"EMA200 full filter: {getattr(cfg, 'ENABLE_200_EMA_FILTER', None)}")
    print(f"EMA200 directional gate: {getattr(cfg, 'ENABLE_EMA200_WATCHLIST', None)}")
    print(f"RVOL: {getattr(cfg, 'ENABLE_RVOL_FILTER', None)} threshold={getattr(cfg, 'RVOL_THRESHOLD', None)}")
    print(f"VWAP acceptance: {getattr(cfg, 'ENABLE_VWAP_ACCEPTANCE_FILTER', True)} bars={getattr(cfg, 'VWAP_ACCEPTANCE_BARS', 2)}")
    print(f"RISK_REWARD_MIN: {getattr(cfg, 'RISK_REWARD_MIN', None)}")
    print(f"ENABLE_FIXED_TARGET: {getattr(cfg, 'ENABLE_FIXED_TARGET', None)} "
          "(True => trailing-stop/structure-break are bypassed by design; only hard-stop + target apply)")
    print("Shortlist:", ", ".join(shortlisted))
    print()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    print("Fetching NIFTY 50 index data once for this run (not re-fetched per symbol, "
          "matching production's real market_df_15m caching)...")
    nifty_15m_raw = fetch_candles(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME,
                                   from_date=start, to_date=end, trim_incomplete=False)
    if nifty_15m_raw.empty:
        raise SystemExit("FATAL: could not fetch NIFTY 50 data -- cannot replay the macro filter honestly.")
    nifty_15m, _ = add_indicators(nifty_15m_raw, nifty_15m_raw.copy(), cfg)
    print(f"NIFTY 50: {len(nifty_15m)} 15-minute candles fetched.\n")

    raw_strategy_signals = []
    gate_passed = []
    rejection_counts = Counter()
    macro_blocked_examples = []
    macro_decision_counts = Counter()

    for idx, symbol in enumerate(shortlisted, 1):
        exchange = exchange_map.get(symbol, "NSE")
        print(f"[{idx:02d}/{len(shortlisted)}] {exchange}:{symbol}", flush=True)
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                     from_date=start, to_date=end, trim_incomplete=False)
            df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                    from_date=start, to_date=end, trim_incomplete=False)
            if df15_raw.empty or df5_raw.empty:
                rejection_counts["NO_DATA"] += 1
                continue

            df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
            today5 = df5[_same_day(df5["date"], TARGET_DATE)]

            for row_index in today5.index:
                candle_ts = df5.loc[row_index, "date"]
                if not _within_entry_window(candle_ts):
                    continue

                # Strict no-lookahead slices: only information available at this 5m candle,
                # including the index data.
                five_slice = df5.loc[:row_index].copy()
                fifteen_slice = df15[df15["date"] <= candle_ts].copy()
                index_slice = nifty_15m[nifty_15m["date"] <= candle_ts].copy()
                if fifteen_slice.empty:
                    rejection_counts["NO_15M_CONTEXT"] += 1
                    continue

                mark_count_before = len(CAPTURED_MARKS)
                sig = strategy_mod.evaluate(symbol, fifteen_slice, five_slice, index_slice, cfg)

                if sig is None:
                    # Attribute this specific rejection to whatever evaluate()
                    # itself reported, not a generic bucket.
                    new_marks = CAPTURED_MARKS[mark_count_before:]
                    reason = new_marks[-1]["status"] if new_marks else "UNKNOWN"
                    rejection_counts[reason] += 1
                    if reason == "MACRO_INDEX_FILTER":
                        d = new_marks[-1]["detail"] if new_marks else {}
                        decision = d.get("decision", "UNKNOWN")
                        macro_decision_counts[decision] += 1
                        if len(macro_blocked_examples) < 10:
                            macro_blocked_examples.append({
                                "symbol": symbol, "time": candle_ts, "detail": d,
                            })
                    continue

                raw_strategy_signals.append(sig)

                eligibility, elig_detail = watchlist_filters_mod.classify_direction_eligibility(
                    fifteen_slice, cfg
                )
                if eligibility not in (watchlist_filters_mod.NOT_ENABLED, sig.direction):
                    rejection_counts["EMA200_DIRECTIONAL"] += 1
                    continue

                rvol_pass, rvol_value, rvol_detail = rvol_mod.passes_rvol_threshold(five_slice, cfg)
                if not rvol_pass:
                    rejection_counts["RVOL"] += 1
                    continue

                gate_passed.append({
                    "symbol": symbol,
                    "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp),
                    "entry": float(sig.entry_price),
                    "stop": float(sig.stop_loss),
                    "target": float(sig.target),
                    "confidence": sig.confidence,
                    "rvol": None if rvol_value is None else float(rvol_value),
                    "ema200_eligibility": eligibility,
                    "reason": sig.reason,
                })

        except Exception as exc:
            rejection_counts["ERROR"] += 1
            print(f"  ERROR: {exc}")

    print("\n" + "=" * 88)
    print("RESULT")
    print("=" * 88)
    print(f"Raw pullback-strategy signals (after time filter + trend/ADX + macro index + pullback "
          f"sequence + VWAP acceptance + full EMA200): {len(raw_strategy_signals)}")
    print(f"Signals also surviving EMA200 directional gate + RVOL: {len(gate_passed)}")

    print("\nREJECTION BREAKDOWN (every candle-evaluation that did NOT produce a signal, "
          "by the specific gate that stopped it):")
    for reason, count in rejection_counts.most_common():
        print(f"  {count:5d}  {reason}")

    print(f"\nMACRO_INDEX_FILTER: {rejection_counts.get('MACRO_INDEX_FILTER', 0)} candle-evaluations "
          "reached this stage (i.e. pullback geometry -- Setup+Rejection+Confirmation+Volume -- "
          "was already fully satisfied). Broken down by decision:")
    for decision, count in macro_decision_counts.most_common():
        print(f"  {count:5d}  {decision}")
    if macro_blocked_examples:
        print("\nUp to 10 examples of pullback sequences the macro layer blocked:")
        for ex in macro_blocked_examples:
            print(f"  {ex['time']:%H:%M}  {ex['symbol']:<14} {ex['detail']}")

    if raw_strategy_signals:
        print("\nRAW PULLBACK-STRATEGY SIGNALS")
        for sig in sorted(raw_strategy_signals, key=lambda s: pd.Timestamp(s.timestamp)):
            print(f"{pd.Timestamp(sig.timestamp):%H:%M}  {sig.symbol:<14} {sig.direction:<4} "
                  f"entry={sig.entry_price:.2f} stop={sig.stop_loss:.2f} target={sig.target:.2f} "
                  f"confidence={sig.confidence}")
    else:
        print("\nNo raw pullback-strategy signals were generated for the production shortlist.")

    if gate_passed:
        print("\nGATE-PASSED SIGNALS (survived every check, ready for exit-replay)")
        for g in sorted(gate_passed, key=lambda s: s["timestamp"]):
            print(f"{g['timestamp']:%H:%M}  {g['symbol']:<14} {g['direction']:<4} "
                  f"entry={g['entry']:.2f} stop={g['stop']:.2f} target={g['target']:.2f} "
                  f"rvol={g['rvol']}")


if __name__ == "__main__":
    main()
