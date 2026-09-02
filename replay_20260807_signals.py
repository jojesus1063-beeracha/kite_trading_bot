"""Read-only replay of 2026-08-07 strategy signals using the exact live bot config.

This script is intentionally safe:
- historical-data reads only;
- never imports executor/order functions;
- never writes trade/position/day state;
- disables filter-diagnostics persistence during replay;
- replays only the production top-N shortlist order from cfg.WATCHLIST.

Run from any isolated clone with BOT_DIR pointing at the real bot directory:
    BOT_DIR=~/kite_trading_bot python3 replay_20260807_signals.py
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

# Historical replay must not pollute runtime/filter_diagnostics/latest.json.
def _noop_mark(*args, **kwargs):
    return None

strategy_mod.mark_filter_status = _noop_mark
rvol_mod.mark_filter_status = _noop_mark
watchlist_filters_mod.mark_filter_status = _noop_mark

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
    print("2026-08-07 HISTORICAL SIGNAL REPLAY — READ ONLY")
    print("=" * 88)
    print(f"BOT_DIR: {BOT_DIR}")
    print(f"Mode in config: {'PAPER' if cfg.PAPER_TRADING else 'LIVE'} (replay itself never places orders)")
    print(f"Watchlist: {len(symbols)} | production shortlist: {len(shortlisted)} | excluded: {len(excluded)}")
    print(f"Entry window: {cfg.NO_ENTRY_BEFORE}–{cfg.NO_ENTRY_AFTER}")
    print(f"EMA200 full filter: {getattr(cfg, 'ENABLE_200_EMA_FILTER', None)}")
    print(f"EMA200 directional gate: {getattr(cfg, 'ENABLE_EMA200_WATCHLIST', None)}")
    print(f"RVOL: {getattr(cfg, 'ENABLE_RVOL_FILTER', None)} threshold={getattr(cfg, 'RVOL_THRESHOLD', None)}")
    print(f"VWAP acceptance: {getattr(cfg, 'ENABLE_VWAP_ACCEPTANCE_FILTER', True)} bars={getattr(cfg, 'VWAP_ACCEPTANCE_BARS', 2)}")
    print("Shortlist:", ", ".join(shortlisted))
    print()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    raw_strategy_signals = []
    gate_passed = []
    rejection_counts = Counter()
    per_symbol = Counter()

# Load NIFTY 50 15m candles once for the entire replay.
# strategy.evaluate() requires benchmark index context.

nifty_raw = fetch_candles(
    kite,
    NIFTY50_TOKEN,
    cfg.TREND_TIMEFRAME,
    from_date=start,
    to_date=end,
    trim_incomplete=False,
)

if nifty_raw.empty:
    raise SystemExit("NIFTY 50 historical data is empty; cannot run replay.")

# strategy.evaluate() expects NIFTY data with EMA indicators.
nifty_15m, _ = add_indicators(nifty_raw, nifty_raw.copy(), cfg)

print(
    f"NIFTY 50 context: {len(nifty_15m)} candles "
    f"from {nifty_15m['date'].min()} to {nifty_15m['date'].max()}"
)

for idx, symbol in enumerate(shortlisted, 1):
    exchange = exchange_map.get(symbol, "NSE")
    print(f"[{idx:02d}/{len(shortlisted)}] {exchange}:{symbol}", flush=True)

    try:
        token = get_instrument_token(kite, symbol, exchange)

        df15_raw = fetch_candles(
            kite,
            token,
            cfg.TREND_TIMEFRAME,
            from_date=start,
            to_date=end,
            trim_incomplete=False,
        )

        df5_raw = fetch_candles(
            kite,
            token,
            cfg.ENTRY_TIMEFRAME,
            from_date=start,
            to_date=end,
            trim_incomplete=False,
        )

        if df15_raw.empty or df5_raw.empty:
            rejection_counts["NO_DATA"] += 1
            continue

        df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
        today5 = df5[_same_day(df5["date"], TARGET_DATE)]

        for row_index in today5.index:
            candle_ts = df5.loc[row_index, "date"]

            if not _within_entry_window(candle_ts):
                continue

            # Strict no-lookahead slices:
            # only information available at this 5m candle.
            five_slice = df5.loc[:row_index].copy()

            fifteen_slice = df15[
                df15["date"] <= candle_ts
            ].copy()

            if fifteen_slice.empty:
                rejection_counts["NO_15M_CONTEXT"] += 1
                continue

            # Use only NIFTY 50 candles available at this exact 5m timestamp.
            index_slice = nifty_15m[
                nifty_15m["date"] <= candle_ts
            ].copy()

            if index_slice.empty:
                rejection_counts["NO_NIFTY_CONTEXT"] += 1
                continue

            sig = strategy_mod.evaluate(
                symbol,
                fifteen_slice,
                five_slice,
                index_slice,
                cfg,
            )

            if sig is None:
                rejection_counts["NO_STRATEGY_SIGNAL"] += 1
                continue

            raw_strategy_signals.append(sig)
            per_symbol[symbol] += 1

            eligibility, elig_detail = (
                watchlist_filters_mod.classify_direction_eligibility(
                    fifteen_slice,
                    cfg,
                )
            )

            if eligibility not in (
                watchlist_filters_mod.NOT_ENABLED,
                sig.direction,
            ):
                rejection_counts["EMA200_DIRECTIONAL"] += 1
                continue

            rvol_pass, rvol_value, rvol_detail = (
                rvol_mod.passes_rvol_threshold(
                    five_slice,
                    cfg,
                )
            )

            if not rvol_pass:
                rejection_counts["RVOL"] += 1
                continue

            gate_passed.append(
                {
                    "symbol": symbol,
                    "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp),
                    "entry": float(sig.entry_price),
                    "stop": float(sig.stop_loss),
                    "target": float(sig.target),
                    "confidence": sig.confidence,
                    "rvol": (
                        None
                        if rvol_value is None
                        else float(rvol_value)
                    ),
                    "ema200_eligibility": eligibility,
                }
            )

    except Exception as exc:
        rejection_counts["ERROR"] += 1
        print(f"  ERROR: {exc}")


print("\n" + "=" * 88)
print("RESULT")
print("=" * 88)

print(
    "Raw strategy signals "
    "(after trend/ADX + 5m EMA/volume + VWAP acceptance + full EMA200): "
    f"{len(raw_strategy_signals)}"
)

print(
    "Signals also surviving EMA200 directional gate + RVOL: "
    f"{len(gate_passed)}"
)

if raw_strategy_signals:
    print("\nRAW STRATEGY SIGNALS")

    for sig in sorted(
        raw_strategy_signals,
        key=lambda s: pd.Timestamp(s.timestamp),
    ):
        print(
            f"{pd.Timestamp(sig.timestamp):%H:%M}  "
            f"{sig.symbol:<14} "
            f"{sig.direction:<4} "
            f"entry={sig.entry_price:.2f} "
            f"stop={sig.stop_loss:.2f} "
            f"target={sig.target:.2f} "
            f"confidence={sig.confidence}"
        )
else:
    print(
        "\nNo raw strategy signals were generated "
        "by the corrected strategy for the production shortlist."
    )

if gate_passed:
    print("\nSURVIVED EMA200-DIRECTIONAL + RVOL")

    for item in sorted(
        gate_passed,
        key=lambda x: x["timestamp"],
    ):
        rv = (
            "N/A"
            if item["rvol"] is None
            else f"{item['rvol']:.2f}"
        )

        print(
            f"{item['timestamp']:%H:%M}  "
            f"{item['symbol']:<14} "
            f"{item['direction']:<4} "
            f"entry={item['entry']:.2f} "
            f"rvol={rv} "
            f"ema200={item['ema200_eligibility']}"
        )
