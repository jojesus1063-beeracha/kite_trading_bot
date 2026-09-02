"""
Entry-timing MEASUREMENT replay for 2026-08-07.

Purpose: determine exactly what each timing filter WOULD block, before
any of them are enabled in production.

CRITICAL DESIGN CHOICE: this script does NOT flip any config flag and
does NOT modify config.py. It calls evaluate_entry_timing() directly
with a local measurement config that has the filters ON, purely to read
the metrics -- while strategy.evaluate() itself continues running with
the real, unmodified production config (all blockers OFF). Signal
generation is therefore provably unaffected: the baseline 6 raw / 4
gate-passed result must reproduce exactly, and this script asserts that.

Reports, per otherwise-valid candidate: ATR, extension in ATR units,
body/range ratio, volume acceleration, each filter's would_pass /
would_block, and the OPTIMAL/ACCEPTABLE/LATE/INVALID grade.

Read-only. Never writes state, never places orders, never mutates cfg.

Run:
    BOT_DIR=~/kite_trading_bot python3 measure_entry_timing_20260807.py
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
from entry_timing import evaluate_entry_timing, OPTIMAL, ACCEPTABLE, LATE, INVALID
from scan_latency import select_scan_universe
from market_trend import NIFTY50_TOKEN

CAPTURED_MARKS = []

def _capturing_mark(symbol, status, detail=None, **kwargs):
    CAPTURED_MARKS.append({"symbol": symbol, "status": status, "detail": detail or {}})
    return None

strategy_mod.mark_filter_status = _capturing_mark
rvol_mod.mark_filter_status = _capturing_mark
watchlist_filters_mod.mark_filter_status = _capturing_mark

TARGET_DATE = pd.Timestamp("2026-08-07")
LOOKBACK_DAYS = 25

BASELINE_RAW = 6
BASELINE_GATE_PASSED = 4


class MeasurementCfg:
    """A LOCAL config used ONLY to read timing metrics. Never assigned
    to the real cfg, never written to disk. Thresholds are the current
    CANDIDATE values -- explicitly not validated trading parameters."""
    ENABLE_ENTRY_TIMING_FILTER = True
    MAX_ENTRY_EXTENSION_ATR = getattr(cfg, "MAX_ENTRY_EXTENSION_ATR", 1.50)
    ATR_PERIOD = getattr(cfg, "ATR_PERIOD", 14)
    ENABLE_CONFIRMATION_QUALITY_FILTER = True
    MIN_CONFIRMATION_BODY_RATIO = getattr(cfg, "MIN_CONFIRMATION_BODY_RATIO", 0.50)
    ENABLE_VOLUME_ACCELERATION_FILTER = True
    MIN_CONFIRMATION_VOLUME_ACCELERATION = getattr(cfg, "MIN_CONFIRMATION_VOLUME_ACCELERATION", 1.10)


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

    print("=" * 100)
    print("ENTRY-TIMING MEASUREMENT REPLAY -- 2026-08-07 -- NO CONFIG CHANGED, NO BLOCKING APPLIED")
    print("=" * 100)
    print("PRODUCTION config (unchanged, used for actual signal generation):")
    print(f"  ENABLE_ENTRY_TIMING_FILTER = {getattr(cfg, 'ENABLE_ENTRY_TIMING_FILTER', None)}")
    print(f"  ENABLE_CONFIRMATION_QUALITY_FILTER = {getattr(cfg, 'ENABLE_CONFIRMATION_QUALITY_FILTER', None)}")
    print(f"  ENABLE_VOLUME_ACCELERATION_FILTER = {getattr(cfg, 'ENABLE_VOLUME_ACCELERATION_FILTER', None)}")
    print("\nMEASUREMENT thresholds (candidate values, NOT validated parameters):")
    print(f"  MAX_ENTRY_EXTENSION_ATR = {MeasurementCfg.MAX_ENTRY_EXTENSION_ATR}")
    print(f"  MIN_CONFIRMATION_BODY_RATIO = {MeasurementCfg.MIN_CONFIRMATION_BODY_RATIO}")
    print(f"  MIN_CONFIRMATION_VOLUME_ACCELERATION = {MeasurementCfg.MIN_CONFIRMATION_VOLUME_ACCELERATION}")
    print()

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    nifty_15m_raw = fetch_candles(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME,
                                   from_date=start, to_date=end, trim_incomplete=False)
    if nifty_15m_raw.empty:
        raise SystemExit("FATAL: could not fetch NIFTY 50 data.")
    nifty_15m, _ = add_indicators(nifty_15m_raw, nifty_15m_raw.copy(), cfg)

    raw_signals = []
    gate_passed = []
    measurements = []
    rejection_counts = Counter()

    for idx, symbol in enumerate(shortlisted, 1):
        exchange = exchange_map.get(symbol, "NSE")
        print(f"[{idx:02d}/{len(shortlisted)}] {symbol}", flush=True)
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                     from_date=start, to_date=end, trim_incomplete=False)
            df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                    from_date=start, to_date=end, trim_incomplete=False)
            if df15_raw.empty or df5_raw.empty:
                continue

            df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
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

                mark_before = len(CAPTURED_MARKS)
                # Real production cfg -- blockers OFF, unchanged.
                sig = strategy_mod.evaluate(symbol, fifteen_slice, five_slice, index_slice, cfg)

                if sig is None:
                    new_marks = CAPTURED_MARKS[mark_before:]
                    rejection_counts[new_marks[-1]["status"] if new_marks else "UNKNOWN"] += 1
                    continue

                raw_signals.append(sig)

                # MEASUREMENT ONLY -- separate local cfg, does not affect sig.
                curr = five_slice.iloc[-1]
                prev = five_slice.iloc[-2] if len(five_slice) >= 2 else None
                timing_class, timing_detail = evaluate_entry_timing(
                    symbol, sig.direction, five_slice, curr, prev, MeasurementCfg()
                )

                anti_chase_pass = timing_detail.get("extension_ok", None)
                conf_pass = (timing_detail.get("confirmation_direction_ok") and
                             timing_detail.get("confirmation_body_ok"))
                vol_pass = timing_detail.get("volume_acceleration_ok", None)

                measurements.append({
                    "symbol": symbol, "timestamp": pd.Timestamp(sig.timestamp),
                    "direction": sig.direction, "entry": float(sig.entry_price),
                    "atr": timing_detail.get("atr"),
                    "extension_atr": timing_detail.get("extension_atr"),
                    "body_ratio": timing_detail.get("body_to_range_ratio"),
                    "volume_acceleration": timing_detail.get("volume_acceleration"),
                    "timing_class": timing_class,
                    "anti_chase_pass": anti_chase_pass,
                    "confirmation_pass": conf_pass,
                    "volume_pass": vol_pass,
                    "blocking_reasons": timing_detail.get("blocking_reasons", []),
                })

                eligibility, _ = watchlist_filters_mod.classify_direction_eligibility(fifteen_slice, cfg)
                if eligibility not in (watchlist_filters_mod.NOT_ENABLED, sig.direction):
                    rejection_counts["EMA200_DIRECTIONAL"] += 1
                    continue
                rvol_pass, rvol_value, _ = rvol_mod.passes_rvol_threshold(five_slice, cfg)
                if not rvol_pass:
                    rejection_counts["RVOL"] += 1
                    continue

                gate_passed.append({
                    "symbol": symbol, "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp), "entry": float(sig.entry_price),
                })

        except Exception as exc:
            rejection_counts["ERROR"] += 1
            print(f"  ERROR: {exc}")

    print("\n" + "=" * 100)
    print("BASELINE INTEGRITY CHECK (Section 12 requirement)")
    print("=" * 100)
    print(f"Raw signals:  {len(raw_signals)}  (baseline {BASELINE_RAW})")
    print(f"Gate-passed:  {len(gate_passed)}  (baseline {BASELINE_GATE_PASSED})")
    if len(raw_signals) != BASELINE_RAW or len(gate_passed) != BASELINE_GATE_PASSED:
        print("\n*** STOP: baseline CHANGED with blockers OFF. Do not enable anything. ***")
    else:
        print("PASS: baseline unchanged -- measurement did not affect signal generation.")

    print("\n" + "=" * 100)
    print("TIMING-IMPACT REPORT")
    print("=" * 100)
    total = len(measurements)
    anti_block = sum(1 for m in measurements if m["anti_chase_pass"] is False)
    conf_block = sum(1 for m in measurements if m["confirmation_pass"] is False)
    vol_block = sum(1 for m in measurements if m["volume_pass"] is False)
    combined_block = sum(1 for m in measurements if m["timing_class"] == INVALID)

    print(f"TOTAL OTHERWISE-VALID CANDIDATES: {total}")
    print(f"\nANTI-CHASE\n    PASS: {total - anti_block}\n    WOULD BLOCK: {anti_block}")
    print(f"\nCONFIRMATION QUALITY\n    PASS: {total - conf_block}\n    WOULD BLOCK: {conf_block}")
    print(f"\nVOLUME ACCELERATION\n    PASS: {total - vol_block}\n    WOULD BLOCK: {vol_block}")
    print(f"\nCOMBINED FILTER EFFECT\n    PASS ALL: {total - combined_block}\n    WOULD BLOCK: {combined_block}")

    grades = Counter(m["timing_class"] for m in measurements)
    print(f"\nTIMING CLASSIFICATION")
    for g in (OPTIMAL, ACCEPTABLE, LATE, INVALID):
        print(f"    {g}: {grades.get(g, 0)}")

    print("\n" + "=" * 100)
    print("PER-CANDIDATE DETAIL")
    print("=" * 100)
    for m in sorted(measurements, key=lambda x: x["timestamp"]):
        print(f"{m['timestamp']:%H:%M}  {m['symbol']:<12} {m['direction']:<4} "
              f"entry={m['entry']:<9.2f} atr={m['atr']} ext_atr={m['extension_atr']} "
              f"body={m['body_ratio']} vol_accel={m['volume_acceleration']}")
        print(f"       anti_chase={'PASS' if m['anti_chase_pass'] else 'FAIL'} "
              f"confirmation={'PASS' if m['confirmation_pass'] else 'FAIL'} "
              f"volume={'PASS' if m['volume_pass'] else 'FAIL'} "
              f"grade={m['timing_class']}"
              + (f" BLOCKED_BY={','.join(m['blocking_reasons'])}" if m['blocking_reasons'] else ""))

    print("\n" + "=" * 100)
    print("BASELINE SIGNALS vs TIMING FILTERS")
    print("=" * 100)
    print(f"BASELINE SIGNALS:              {len(raw_signals)}")
    print(f"NEW TIMING FILTERS WOULD REMOVE: {combined_block}")
    print(f"SIGNALS REMAINING:             {len(raw_signals) - combined_block}")


if __name__ == "__main__":
    main()
