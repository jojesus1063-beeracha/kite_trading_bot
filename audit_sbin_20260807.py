"""
Focused technical audit: traces SBIN candle-by-candle through the full
production pullback pipeline for 2026-08-07, reporting the actual
pass/fail state of EVERY gate (not just the first one that blocked),
using the real evaluate() and its internal mark_filter_status() calls.

Read-only. Never writes state, never places orders.

Run:
    BOT_DIR=~/kite_trading_bot python3 audit_sbin_20260807.py
"""

from __future__ import annotations

import os
import sys
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
from market_trend import NIFTY50_TOKEN

SYMBOL = "SBIN"
EXCHANGE = "NSE"
TARGET_DATE = pd.Timestamp("2026-08-07")
LOOKBACK_DAYS = 25

CAPTURED = []

def _capturing_mark(symbol, status, detail=None, **kwargs):
    CAPTURED.append({"symbol": symbol, "status": status, "detail": detail or {}})
    return None

strategy_mod.mark_filter_status = _capturing_mark


def _within_entry_window(ts):
    t = pd.Timestamp(ts).time()
    start = datetime.strptime(cfg.NO_ENTRY_BEFORE, "%H:%M").time()
    end = datetime.strptime(cfg.NO_ENTRY_AFTER, "%H:%M").time()
    return start <= t <= end


def main():
    kite = get_kite_client()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    print("=" * 90)
    print(f"TECHNICAL AUDIT: {SYMBOL} -- 2026-08-07 -- FULL PIPELINE TRACE")
    print("=" * 90)
    print(f"RISK_REWARD_MIN={getattr(cfg,'RISK_REWARD_MIN',None)} "
          f"ENABLE_FIXED_TARGET={getattr(cfg,'ENABLE_FIXED_TARGET',None)} "
          f"VOLUME_MULTIPLIER={getattr(cfg,'VOLUME_MULTIPLIER',None)}")
    print()

    print("Fetching NIFTY 50 index data...")
    nifty_15m_raw = fetch_candles(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME,
                                   from_date=start, to_date=end, trim_incomplete=False)
    nifty_15m, _ = add_indicators(nifty_15m_raw, nifty_15m_raw.copy(), cfg)
    print(f"NIFTY 50: {len(nifty_15m)} candles.\n")

    print(f"Fetching {SYMBOL} data...")
    token = get_instrument_token(kite, SYMBOL, EXCHANGE)
    df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                             from_date=start, to_date=end, trim_incomplete=False)
    df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                            from_date=start, to_date=end, trim_incomplete=False)
    if df15_raw.empty or df5_raw.empty:
        raise SystemExit(f"FATAL: no data available for {SYMBOL} on {TARGET_DATE.date()}.")

    df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
    today5 = df5[df5["date"].dt.date == TARGET_DATE.date()]
    print(f"{SYMBOL}: {len(today5)} candles on {TARGET_DATE.date()}.\n")

    print(f"Day's actual price action: open={today5.iloc[0]['open']:.2f} "
          f"high={today5['high'].max():.2f} low={today5['low'].min():.2f} "
          f"close={today5.iloc[-1]['close']:.2f} "
          f"(close vs open: {(today5.iloc[-1]['close']/today5.iloc[0]['open']-1)*100:+.2f}%)\n")

    print("=" * 90)
    print("CANDLE-BY-CANDLE TRACE")
    print("=" * 90)

    rows_evaluated = 0
    for row_index in today5.index:
        candle_ts = df5.loc[row_index, "date"]
        if not _within_entry_window(candle_ts):
            continue
        if row_index < 1:
            continue

        rows_evaluated += 1
        five_slice = df5.loc[:row_index].copy()
        fifteen_slice = df15[df15["date"] <= candle_ts].copy()
        index_slice = nifty_15m[nifty_15m["date"] <= candle_ts].copy()

        curr = df5.loc[row_index]
        prev = df5.loc[row_index - 1] if row_index - 1 in df5.index else None

        mark_before = len(CAPTURED)
        sig = strategy_mod.evaluate(SYMBOL, fifteen_slice, five_slice, index_slice, cfg)
        new_marks = CAPTURED[mark_before:]

        if sig is not None:
            print(f"{candle_ts:%H:%M}  *** SIGNAL PRODUCED: {sig.direction} entry={sig.entry_price:.2f} "
                  f"stop={sig.stop_loss:.2f} target={sig.target:.2f} ***")
            continue

        if not new_marks:
            continue
        reason = new_marks[-1]["status"]
        detail = new_marks[-1]["detail"]

        if reason == "PULLBACK_SEQUENCE":
            # Index fields no longer live here -- macro authorization is
            # now a separate, later step (see MACRO_INDEX_FILTER below).
            d = detail
            print(f"{candle_ts:%H:%M}  PULLBACK_SEQUENCE FAIL dir={d.get('direction')} "
                  f"setup={d.get('setup')} rejection={d.get('rejection')} "
                  f"confirmation={d.get('confirmation')} volume_ok={d.get('volume_ok')} "
                  f"| close={curr['close']:.2f} ema_entry={curr['ema_entry']:.2f} "
                  f"prev_close={prev['close']:.2f} prev_high={prev['high']:.2f} prev_low={prev['low']:.2f}")
        elif reason == "TREND_OR_ADX":
            print(f"{candle_ts:%H:%M}  TREND_OR_ADX FAIL | 15m close/ema_fast/ema_slow trend not established "
                  f"(no Setup/Rejection/Confirmation evaluation ever reached for this candle)")
        elif reason == "MACRO_INDEX_FILTER":
            d = detail
            decision = d.get("decision")
            if decision == "HARD_REJECT":
                print(f"{candle_ts:%H:%M}  MACRO_INDEX_FILTER: pullback geometry PASSED, but HARD_REJECT "
                      f"| macro_state={d.get('macro_state')} direction={d.get('direction')} "
                      f"reason={d.get('reason', d.get('decision'))}")
            elif decision == "CONDITIONAL_REJECTED":
                print(f"{candle_ts:%H:%M}  MACRO_INDEX_FILTER: pullback geometry PASSED, NIFTY NEUTRAL, "
                      f"CONDITIONAL_REJECTED | direction={d.get('direction')} "
                      f"adx_ok={d.get('adx_ok')} (adx={d.get('adx_value')}, threshold={d.get('adx_threshold')}) "
                      f"ema_slope_ok={d.get('ema_slope_ok')} (slope={d.get('ema_slope_value')})")
            else:
                print(f"{candle_ts:%H:%M}  MACRO_INDEX_FILTER FAIL | {d.get('reason', d)} "
                      f"(pullback geometry was never evaluated -- blocked before that point)")
        elif reason == "VWAP_ACCEPTANCE":
            print(f"{candle_ts:%H:%M}  VWAP_ACCEPTANCE FAIL dir={detail.get('direction')} "
                  f"(pullback sequence AND macro authorization both DID pass)")
        elif reason == "EMA200_CONFIRMATION":
            print(f"{candle_ts:%H:%M}  EMA200_CONFIRMATION FAIL dir={detail.get('direction')} "
                  f"detail={detail} (pullback sequence + macro authorization + VWAP acceptance all DID pass)")
        else:
            print(f"{candle_ts:%H:%M}  {reason} FAIL | {detail}")

    print(f"\nTotal candles evaluated within entry window: {rows_evaluated}")


if __name__ == "__main__":
    main()
