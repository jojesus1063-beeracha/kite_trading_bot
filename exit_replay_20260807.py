"""Read-only exit replay for 2026-08-07 missed eligible signals.

Purpose
-------
Regenerate the corrected Aug-07 signal set from real Zerodha historical
candles, apply the same fixed stop/target formula production uses after a
paper fill, deduplicate same-symbol signals while a hypothetical position is
open, and measure what happened afterwards.

Important fidelity limit
------------------------
Production position monitoring polls roughly every POSITION_CHECK_SECONDS and
uses the *current close of the still-forming 5-minute candle* (because
check_position_exit(..., trim_incomplete=False) reads df_5m.iloc[-1]["close"]).
Zerodha historical API does not provide the old 25-second snapshots. Therefore
an exact 25-second replay is impossible after the fact unless tick/quote data
was separately recorded.

This script uses real 1-minute historical CLOSES as the closest reproducible
proxy for production exit observations. It also reports 1-minute high/low MFE
and MAE as excursion analytics only; highs/lows do NOT trigger the simulated
production exit.

Safety
------
- historical reads only
- never imports executor/order-placement code
- never writes bot state, trade history, positions or day state
- disables filter-diagnostics persistence during replay

Run from the isolated diagnostics clone:
    BOT_DIR=~/kite_trading_bot ~/kite_trading_bot/venv/bin/python \
        exit_replay_20260807.py 2>&1 | tee /tmp/exit_replay_20260807_output.txt
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
from trade_levels import fixed_levels_from_fill

# auth.py uses cfg.ACCESS_TOKEN_FILE as a path. Make it absolute so the replay
# can run safely from the isolated clone without copying/symlinking the token.
_token_path = Path(cfg.ACCESS_TOKEN_FILE)
if not _token_path.is_absolute():
    cfg.ACCESS_TOKEN_FILE = str(BOT_DIR / _token_path)

# Historical replay must never pollute runtime/filter_diagnostics/latest.json.
def _noop_mark(*args, **kwargs):
    return None

strategy_mod.mark_filter_status = _noop_mark
rvol_mod.mark_filter_status = _noop_mark
watchlist_filters_mod.mark_filter_status = _noop_mark

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


def _same_day(series, target):
    return series.dt.date == target.date()


def _within_entry_window(ts):
    t = pd.Timestamp(ts).time()
    start = datetime.strptime(cfg.NO_ENTRY_BEFORE, "%H:%M").time()
    end = datetime.strptime(cfg.NO_ENTRY_AFTER, "%H:%M").time()
    return start <= t <= end


def _signal_available_at(signal_ts):
    """strategy.Signal.timestamp is the 5-minute candle start time.

    Production cannot act until that candle is completed. Use candle close as
    the earliest reproducible entry-availability time. The real scheduler then
    adds its scan buffer/order latency; that sub-minute delay is not available
    from historical candles and is deliberately not fabricated here.
    """
    return pd.Timestamp(signal_ts) + pd.Timedelta(minutes=5)


def _coerce_to_reference_tz(ts, reference_series):
    ts = pd.Timestamp(ts)
    if reference_series.empty:
        return ts
    ref = pd.Timestamp(reference_series.iloc[0])
    if ref.tzinfo is not None and ts.tzinfo is None:
        return ts.tz_localize(ref.tzinfo)
    if ref.tzinfo is None and ts.tzinfo is not None:
        return ts.tz_localize(None)
    if ref.tzinfo is not None and ts.tzinfo is not None:
        return ts.tz_convert(ref.tzinfo)
    return ts


def _production_levels(candidate):
    """Return the exact levels production would store after a paper fill."""
    if not getattr(cfg, "ENABLE_FIXED_TARGET", False):
        raise RuntimeError(
            "This replay is intentionally scoped to the current fixed-target "
            "production mode. ENABLE_FIXED_TARGET is False; aborting rather "
            "than fabricating the trailing/structure/trend exit stack."
        )

    entry = float(candidate["entry"])
    stop_pct = float(cfg.STOP_LOSS_PERCENT)
    target_pct = float(cfg.PROFIT_TARGET_PERCENT)
    stop, target = fixed_levels_from_fill(
        candidate["direction"], entry, stop_pct, target_pct
    )
    return float(stop), float(target)


def _move_pct(direction, entry, price):
    if direction == "BUY":
        return (float(price) - entry) / entry * 100.0
    return (entry - float(price)) / entry * 100.0


def _walk_exit(candidate, minute_df):
    """Walk real 1-minute closes as a reproducible proxy for live 25s checks.

    Exit priority matches main.check_position_exit fixed-target mode:
      1. hard stop
      2. fixed target

    Intraminute high/low are used only for MFE/MAE and touch diagnostics.
    """
    symbol = candidate["symbol"]
    direction = candidate["direction"]
    entry = float(candidate["entry"])
    signal_ts = pd.Timestamp(candidate["timestamp"])
    entry_time = _signal_available_at(signal_ts)
    stop, target = _production_levels(candidate)

    if minute_df is None or minute_df.empty:
        return {
            **candidate,
            "entry_time": entry_time,
            "production_stop": stop,
            "production_target": target,
            "outcome": "NO_MINUTE_DATA",
            "exit_time": None,
            "exit_price": None,
            "gross_pnl_per_share": None,
            "mfe_pct": None,
            "mae_pct": None,
            "target_touched_intraminute": False,
            "stop_touched_intraminute": False,
        }

    entry_time = _coerce_to_reference_tz(entry_time, minute_df["date"])
    square_time = datetime.strptime(cfg.FORCE_SQUARE_OFF_TIME, "%H:%M").time()

    rows = minute_df[
        (_same_day(minute_df["date"], TARGET_DATE))
        & (minute_df["date"] >= entry_time)
        & (minute_df["date"].dt.time <= square_time)
    ].copy()

    if rows.empty:
        return {
            **candidate,
            "entry_time": entry_time,
            "production_stop": stop,
            "production_target": target,
            "outcome": "NO_POST_ENTRY_DATA",
            "exit_time": None,
            "exit_price": None,
            "gross_pnl_per_share": None,
            "mfe_pct": None,
            "mae_pct": None,
            "target_touched_intraminute": False,
            "stop_touched_intraminute": False,
        }

    best_favorable = 0.0
    worst_adverse = 0.0
    target_touch = False
    stop_touch = False
    target_touch_time = None
    stop_touch_time = None

    outcome = None
    exit_time = None
    exit_price = None

    for _, row in rows.iterrows():
        ts = pd.Timestamp(row["date"])
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])

        if direction == "BUY":
            favorable = _move_pct(direction, entry, high)
            adverse = _move_pct(direction, entry, low)
            if high >= target and not target_touch:
                target_touch = True
                target_touch_time = ts
            if low <= stop and not stop_touch:
                stop_touch = True
                stop_touch_time = ts
            hit_stop_close = close <= stop
            hit_target_close = close >= target
        else:
            favorable = _move_pct(direction, entry, low)
            adverse = _move_pct(direction, entry, high)
            if low <= target and not target_touch:
                target_touch = True
                target_touch_time = ts
            if high >= stop and not stop_touch:
                stop_touch = True
                stop_touch_time = ts
            hit_stop_close = close >= stop
            hit_target_close = close <= target

        best_favorable = max(best_favorable, favorable)
        worst_adverse = min(worst_adverse, adverse)

        # Production check_position_exit evaluates hard stop before target.
        if hit_stop_close:
            outcome = "STOP_CLOSE_PROXY"
            exit_time = ts
            exit_price = close
            break
        if hit_target_close:
            outcome = "TARGET_CLOSE_PROXY"
            exit_time = ts
            exit_price = close
            break

    if outcome is None:
        # Production force-square-off happens at/after FORCE_SQUARE_OFF_TIME.
        # Historical 1-minute data cannot reconstruct the exact market fill;
        # use the last available 1-minute close at/before cutoff as a proxy.
        last = rows.iloc[-1]
        outcome = "SQUARE_OFF_CLOSE_PROXY"
        exit_time = pd.Timestamp(last["date"])
        exit_price = float(last["close"])

    gross = _move_pct(direction, entry, exit_price) * entry / 100.0

    return {
        **candidate,
        "entry_time": entry_time,
        "production_stop": stop,
        "production_target": target,
        "outcome": outcome,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "gross_pnl_per_share": gross,
        "mfe_pct": best_favorable,
        "mae_pct": worst_adverse,
        "target_touched_intraminute": target_touch,
        "target_touch_time": target_touch_time,
        "stop_touched_intraminute": stop_touch,
        "stop_touch_time": stop_touch_time,
    }


def _generate_candidates(kite):
    all_rows = _watchlist_rows()
    symbols = [s for s, _ in all_rows]
    exchange_map = dict(all_rows)
    shortlisted, _, excluded = select_scan_universe(
        symbols, [], getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30)
    )

    start = TARGET_DATE.to_pydatetime() - timedelta(days=LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    raw_count = 0
    candidates = []
    minute_cache = {}
    rejection_counts = Counter()

    print("Shortlist:", ", ".join(shortlisted))
    print()

    for idx, symbol in enumerate(shortlisted, 1):
        exchange = exchange_map.get(symbol, "NSE")
        print(f"[{idx:02d}/{len(shortlisted)}] {exchange}:{symbol}", flush=True)
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(
                kite, token, cfg.TREND_TIMEFRAME,
                from_date=start, to_date=end, trim_incomplete=False,
            )
            df5_raw = fetch_candles(
                kite, token, cfg.ENTRY_TIMEFRAME,
                from_date=start, to_date=end, trim_incomplete=False,
            )
            minute_raw = fetch_candles(
                kite, token, "minute",
                from_date=TARGET_DATE.to_pydatetime(),
                to_date=end, trim_incomplete=False,
            )
            minute_cache[symbol] = minute_raw

            if df15_raw.empty or df5_raw.empty:
                rejection_counts["NO_DATA"] += 1
                continue

            df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
            today5 = df5[_same_day(df5["date"], TARGET_DATE)]

            for row_index in today5.index:
                candle_ts = pd.Timestamp(df5.loc[row_index, "date"])
                if not _within_entry_window(candle_ts):
                    continue

                five_slice = df5.loc[:row_index].copy()
                fifteen_slice = df15[df15["date"] <= candle_ts].copy()
                if fifteen_slice.empty:
                    rejection_counts["NO_15M_CONTEXT"] += 1
                    continue

                sig = strategy_mod.evaluate(symbol, fifteen_slice, five_slice, cfg)
                if sig is None:
                    rejection_counts["NO_STRATEGY_SIGNAL"] += 1
                    continue

                raw_count += 1

                eligibility, _ = watchlist_filters_mod.classify_direction_eligibility(
                    fifteen_slice, cfg
                )
                if eligibility not in (watchlist_filters_mod.NOT_ENABLED, sig.direction):
                    rejection_counts["EMA200_DIRECTIONAL"] += 1
                    continue

                rvol_pass, rvol_value, _ = rvol_mod.passes_rvol_threshold(five_slice, cfg)
                if not rvol_pass:
                    rejection_counts["RVOL"] += 1
                    continue

                candidates.append({
                    "symbol": symbol,
                    "exchange": exchange,
                    "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp),
                    "entry": float(sig.entry_price),
                    "signal_stop": float(sig.stop_loss),
                    "signal_target": float(sig.target),
                    "confidence": sig.confidence,
                    "rvol": None if rvol_value is None else float(rvol_value),
                    "ema200_eligibility": eligibility,
                })

        except Exception as exc:
            rejection_counts["ERROR"] += 1
            print(f"  ERROR: {exc}")

    return {
        "watchlist_size": len(symbols),
        "shortlist_size": len(shortlisted),
        "excluded_size": len(excluded),
        "raw_count": raw_count,
        "candidates": candidates,
        "minute_cache": minute_cache,
        "rejections": rejection_counts,
    }


def _dedupe_and_walk(candidates, minute_cache):
    results = []
    skipped = []
    open_until = {}

    ordered = sorted(
        candidates,
        key=lambda x: (_signal_available_at(x["timestamp"]), x["symbol"]),
    )

    for cand in ordered:
        symbol = cand["symbol"]
        available_at = _signal_available_at(cand["timestamp"])
        prior_exit = open_until.get(symbol)

        if prior_exit is not None:
            # Align timestamp timezone before comparing.
            compare_available = pd.Timestamp(available_at)
            compare_exit = pd.Timestamp(prior_exit)
            if compare_exit.tzinfo is not None and compare_available.tzinfo is None:
                compare_available = compare_available.tz_localize(compare_exit.tzinfo)
            elif compare_exit.tzinfo is None and compare_available.tzinfo is not None:
                compare_available = compare_available.tz_localize(None)
            elif compare_exit.tzinfo is not None and compare_available.tzinfo is not None:
                compare_available = compare_available.tz_convert(compare_exit.tzinfo)

            if compare_available <= compare_exit:
                skipped.append({**cand, "skip_reason": "SAME_SYMBOL_POSITION_STILL_OPEN"})
                continue

        result = _walk_exit(cand, minute_cache.get(symbol))
        results.append(result)

        if result.get("exit_time") is not None:
            open_until[symbol] = result["exit_time"]
        else:
            # No determinable exit => conservatively block later same-symbol entries.
            open_until[symbol] = pd.Timestamp(TARGET_DATE) + pd.Timedelta(days=1)

    return results, skipped


def _fmt(value, digits=2):
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.{digits}f}"


def main():
    if not getattr(cfg, "PAPER_TRADING", False):
        print("WARNING: effective config is LIVE, but this replay remains read-only and places no orders.")

    if not getattr(cfg, "ENABLE_FIXED_TARGET", False):
        raise SystemExit(
            "ENABLE_FIXED_TARGET=False. This script intentionally refuses to pretend the "
            "fixed-target replay covers trailing/structure/trend exits."
        )

    print("=" * 100)
    print("2026-08-07 MISSED-SIGNAL EXIT REPLAY — READ ONLY")
    print("=" * 100)
    print(f"BOT_DIR: {BOT_DIR}")
    print(f"Effective PAPER_TRADING: {cfg.PAPER_TRADING}")
    print(f"Entry window: {cfg.NO_ENTRY_BEFORE}–{cfg.NO_ENTRY_AFTER}")
    print(f"Square-off: {cfg.FORCE_SQUARE_OFF_TIME}")
    print(f"Fixed target: {cfg.ENABLE_FIXED_TARGET}")
    print(f"Production stop: {cfg.STOP_LOSS_PERCENT}% from paper fill")
    print(f"Production target: {cfg.PROFIT_TARGET_PERCENT}% from paper fill")
    print(f"Position monitor interval: {getattr(cfg, 'POSITION_CHECK_SECONDS', 'N/A')}s")
    print()
    print("FIDELITY NOTE: production checks the live current close about every 25s. Historical")
    print("Kite data cannot reconstruct those old 25-second snapshots. Exit decisions below use")
    print("real 1-minute CLOSES as the closest reproducible proxy. 1-minute highs/lows are")
    print("reported only as MFE/MAE/touch evidence and never used to trigger the proxy exit.")
    print()

    kite = get_kite_client()
    generated = _generate_candidates(kite)
    candidates = generated["candidates"]

    results, skipped = _dedupe_and_walk(candidates, generated["minute_cache"])

    print("\n" + "=" * 100)
    print("RESULT")
    print("=" * 100)
    print(f"Raw strategy signals: {generated['raw_count']}")
    print(f"Survived EMA200 directional + RVOL: {len(candidates)}")
    print(f"Deduplicated hypothetical trades: {len(results)}")
    print(f"Skipped overlapping same-symbol signals: {len(skipped)}")

    print("\nTRADE-BY-TRADE RESULTS")
    print("-" * 100)
    for r in results:
        sig_time = pd.Timestamp(r["timestamp"])
        ent_time = pd.Timestamp(r["entry_time"])
        ex_time = r.get("exit_time")
        ex_text = "N/A" if ex_time is None else pd.Timestamp(ex_time).strftime("%H:%M")
        print(
            f"{sig_time:%H:%M} candle | entry~{ent_time:%H:%M} | {r['symbol']:<12} {r['direction']:<4} "
            f"entry={r['entry']:.2f} prod_stop={r['production_stop']:.2f} "
            f"prod_target={r['production_target']:.2f} | {r['outcome']:<23} "
            f"exit={_fmt(r.get('exit_price'))}@{ex_text} | "
            f"MFE={_fmt(r.get('mfe_pct'))}% MAE={_fmt(r.get('mae_pct'))}% | "
            f"gross/share={_fmt(r.get('gross_pnl_per_share'))}"
        )
        if r.get("target_touched_intraminute") and r["outcome"] != "TARGET_CLOSE_PROXY":
            tt = r.get("target_touch_time")
            print(f"    NOTE: target was touched by a 1m HIGH/LOW at {pd.Timestamp(tt):%H:%M} but not confirmed by proxy close before exit.")
        if r.get("stop_touched_intraminute") and r["outcome"] != "STOP_CLOSE_PROXY":
            st = r.get("stop_touch_time")
            print(f"    NOTE: stop was touched by a 1m HIGH/LOW at {pd.Timestamp(st):%H:%M} but not confirmed by proxy close before exit.")

    counts = Counter(r["outcome"] for r in results)
    valid_pnls = [r["gross_pnl_per_share"] for r in results if r.get("gross_pnl_per_share") is not None]
    target_count = counts.get("TARGET_CLOSE_PROXY", 0)
    stop_count = counts.get("STOP_CLOSE_PROXY", 0)
    square_count = counts.get("SQUARE_OFF_CLOSE_PROXY", 0)
    denom = target_count + stop_count + square_count

    print("\nSUMMARY")
    print("-" * 100)
    print(f"TARGET_CLOSE_PROXY: {target_count}")
    print(f"STOP_CLOSE_PROXY: {stop_count}")
    print(f"SQUARE_OFF_CLOSE_PROXY: {square_count}")
    for k, v in counts.items():
        if k not in {"TARGET_CLOSE_PROXY", "STOP_CLOSE_PROXY", "SQUARE_OFF_CLOSE_PROXY"}:
            print(f"{k}: {v}")
    print(f"Target-close rate: {(100.0 * target_count / denom):.2f}%" if denom else "Target-close rate: N/A")
    print(f"Stop-close rate: {(100.0 * stop_count / denom):.2f}%" if denom else "Stop-close rate: N/A")
    if valid_pnls:
        print(f"Gross P&L, one share per deduplicated trade: Rs{sum(valid_pnls):.2f}")
        print(f"Average gross P&L per share/trade: Rs{sum(valid_pnls)/len(valid_pnls):.2f}")
    target_touches = sum(bool(r.get("target_touched_intraminute")) for r in results)
    stop_touches = sum(bool(r.get("stop_touched_intraminute")) for r in results)
    print(f"Trades whose 1m HIGH/LOW touched target at any point before proxy exit: {target_touches}")
    print(f"Trades whose 1m HIGH/LOW touched stop at any point before proxy exit: {stop_touches}")

    if results:
        best = max(results, key=lambda r: r.get("mfe_pct") if r.get("mfe_pct") is not None else float("-inf"))
        worst = min(results, key=lambda r: r.get("mae_pct") if r.get("mae_pct") is not None else float("inf"))
        print(f"Best MFE: {best['symbol']} {best['direction']} {_fmt(best.get('mfe_pct'))}%")
        print(f"Worst MAE: {worst['symbol']} {worst['direction']} {_fmt(worst.get('mae_pct'))}%")

    print("\nREJECTION COUNTS DURING SIGNAL REGENERATION")
    for reason, count in generated["rejections"].most_common():
        print(f"{count:5d}  {reason}")

    print("\nINTERPRETATION LIMIT")
    print("These are historical 1-minute-close proxy outcomes for the production fixed-target logic.")
    print("They are stronger evidence than a 5-minute-final-close backtest, but they are NOT an exact")
    print("reconstruction of the live 25-second monitor. Exact historical 25-second outcomes require")
    print("recorded ticks/quotes from the session. Production-sized P&L is also not claimed here because")
    print("historical live margin, simultaneous-position ranking and margin caps are not reproducible exactly.")


if __name__ == "__main__":
    main()
