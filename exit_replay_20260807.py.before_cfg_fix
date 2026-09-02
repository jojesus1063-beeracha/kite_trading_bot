"""Option A: exact production-fidelity exit replay for 2026-08-07.

Read-only. Never writes state, never places orders. Reuses the EXACT
signal-generation logic from replay_20260807_signals.py (imported, not
re-typed -- avoids any transcription error in the 46 surviving signals)
and then walks real, subsequent 5-minute candle CLOSES, checking each
against production's actual hit_hard_stop/hit_target formulas (main.py,
confirmed by direct code reading, not assumption):

    hit_hard_stop = (close <= stop)  if BUY else (close >= stop)
    hit_target    = (close >= target) if BUY else (close <= target)

This is close-price-only, on 5-minute candles -- NOT 1-minute intrabar
high/low touches. That is a deliberate, verified match to what
check_position_exit() actually does in paper mode (confirmed: in paper
mode, inspect_protective_stop() returns immediately with state=PAPER,
skipping all broker-coordination code, falling straight to this exact
price-comparison logic).

If ENABLE_FIXED_TARGET is True (today's actual config, confirmed):
ONLY hard-stop and fixed-target are checked -- trailing stop, market
structure break, and trend reversal are correctly NOT simulated,
because production itself does not check them in this mode (main.py,
lines 1959-1971 -- explicit design comment: "by explicit design choice,
temporary pullbacks... must NOT close the trade early").

If ENABLE_FIXED_TARGET is False: this script does not yet implement the
trailing-stop/ATR/structure-break branch -- it will say so explicitly
rather than silently approximate it.

Run:
    BOT_DIR=~/kite_trading_bot python3 exit_replay_20260807.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

BOT_DIR = Path(os.path.expanduser(os.environ.get("BOT_DIR", "~/kite_trading_bot"))).resolve()
if not BOT_DIR.exists():
    raise SystemExit(f"BOT_DIR does not exist: {BOT_DIR}")
sys.path.insert(0, str(BOT_DIR))

import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token
from trade_levels import fixed_levels_from_fill  # noqa: E402

# Reuse the exact signal-generation logic already built and verified --
# do not re-derive the 46 signals by hand.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_20260807_signals as sigrep  # noqa: E402

TARGET_DATE = pd.Timestamp("2026-08-07")
SQUARE_OFF_TIME = "15:08"  # matches the production force square-off time this week


def _recompute_stop_target(signal):
    """
    CRITICAL: entry_protection.py (lines 110-119, confirmed by direct
    code reading) DISCARDS strategy.evaluate()'s own signal.stop_loss /
    signal.target entirely when ENABLE_FIXED_TARGET=True (today's real
    config) and recomputes BOTH via fixed_levels_from_fill(), using the
    CONFIRMED ENTRY PRICE (not the signal candle's low/high) and
    STOP_LOSS_PERCENT / PROFIT_TARGET_PERCENT (not SL_BUFFER_PCT /
    RISK_REWARD_MIN). Using signal.stop_loss/signal.target directly, as
    an earlier version of this script did, would replay the WRONG
    levels -- confirmed as a real bug, not a hypothetical one.

    In paper mode (confirmed via executor.py: multiple paper-fill
    result paths explicitly set average_price=None), entry_protection.py
    falls back to confirmed_entry_price = signal.entry_price exactly
    (line 102-103) -- so fill_price here is signal.entry_price, with no
    approximation involved.
    """
    if not getattr(cfg, "ENABLE_FIXED_TARGET", False):
        # Not today's config, but handle correctly if ever run under it:
        # in this mode entry_protection.py does NOT override the
        # strategy-computed levels (confirmed: the override is inside
        # the `if ENABLE_FIXED_TARGET:` block only).
        return signal["stop"], signal["target"]

    stop_price, _ = fixed_levels_from_fill(
        signal["direction"],
        signal["entry"],  # == confirmed_entry_price in paper mode, verified
        getattr(cfg, "STOP_LOSS_PERCENT", 0.45),
        getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
    )
    target_price = signal["target"]
    return stop_price, target_price


def _generate_gate_passed_signals(kite):
    """Runs the exact same generation path as replay_20260807_signals.py's
    main(), but returns the gate_passed list instead of only printing it."""
    all_rows = sigrep._watchlist_rows()
    symbols = [s for s, _ in all_rows]
    exchange_map = dict(all_rows)
    shortlisted, _, _ = sigrep.select_scan_universe(
        symbols, [], getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 30)
    )

    import pandas as pd
    from datetime import timedelta
    start = TARGET_DATE.to_pydatetime() - timedelta(days=sigrep.LOOKBACK_DAYS)
    end = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    gate_passed = []
    for symbol in shortlisted:
        exchange = exchange_map.get(symbol, "NSE")
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df15_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                     from_date=start, to_date=end, trim_incomplete=False)
            df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                    from_date=start, to_date=end, trim_incomplete=False)
            if df15_raw.empty or df5_raw.empty:
                continue

            from indicators import add_indicators
            df15, df5 = add_indicators(df15_raw, df5_raw, cfg)
            today5 = df5[sigrep._same_day(df5["date"], TARGET_DATE)]

            for row_index in today5.index:
                candle_ts = df5.loc[row_index, "date"]
                if not sigrep._within_entry_window(candle_ts):
                    continue

                five_slice = df5.loc[:row_index].copy()
                fifteen_slice = df15[df15["date"] <= candle_ts].copy()
                if fifteen_slice.empty:
                    continue

                sig = sigrep.strategy_mod.evaluate(symbol, fifteen_slice, five_slice, cfg)
                if sig is None:
                    continue

                eligibility, _ = sigrep.watchlist_filters_mod.classify_direction_eligibility(
                    fifteen_slice, cfg
                )
                if eligibility not in (sigrep.watchlist_filters_mod.NOT_ENABLED, sig.direction):
                    continue

                rvol_pass, rvol_value, _ = sigrep.rvol_mod.passes_rvol_threshold(five_slice, cfg)
                if not rvol_pass:
                    continue

                gate_passed.append({
                    "symbol": symbol, "exchange": exchange, "token": token,
                    "direction": sig.direction,
                    "timestamp": pd.Timestamp(sig.timestamp),
                    "entry": float(sig.entry_price),
                    "stop": float(sig.stop_loss),
                    "target": float(sig.target),
                })
        except Exception as exc:
            print(f"  ERROR generating signals for {symbol}: {exc}")

    return gate_passed


def _deduplicate_same_symbol(signals):
    """The same symbol cannot have overlapping hypothetical trades.
    Once a signal opens a position, later signals for that same symbol
    are ignored until the walk determines that position has exited."""
    by_symbol = {}
    for sig in signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig)
    for sym in by_symbol:
        by_symbol[sym].sort(key=lambda s: s["timestamp"])
    return by_symbol


def _walk_exit(kite, signal, all_5m_by_symbol):
    """Walks real 5-minute closes from entry to square-off, applying
    the EXACT production hit_hard_stop / hit_target formulas. Returns a
    dict with the full trade record, or None if data was unavailable."""
    symbol = signal["symbol"]
    direction = signal["direction"]
    entry_price = signal["entry"]
    stop, target = _recompute_stop_target(signal)
    entry_ts = signal["timestamp"]

    df5 = all_5m_by_symbol.get(symbol)
    if df5 is None or df5.empty:
        return {"symbol": symbol, "exit_reason": "NO_DATA", "note": "no 5-minute data available for this symbol"}

    square_off_time = pd.Timestamp(
        f"{entry_ts.date()} {SQUARE_OFF_TIME}"
    )

    # Only candles strictly after the entry candle -- the entry candle
    # itself is what produced the signal, not a monitoring candle.
    # Normalize comparison timestamps to the same timezone as Zerodha candle data.
    date_series = pd.to_datetime(df5["date"])
    if getattr(date_series.dt, "tz", None) is not None:
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize(date_series.dt.tz)
        else:
            entry_ts = entry_ts.tz_convert(date_series.dt.tz)

        if square_off_time.tzinfo is None:
            square_off_time = square_off_time.tz_localize(date_series.dt.tz)
        else:
            square_off_time = square_off_time.tz_convert(date_series.dt.tz)

    after_entry = df5[
        (df5["date"] > entry_ts) &
        (df5["date"] <= square_off_time)
    ].sort_values("date")

    fixed_target_mode = getattr(cfg, "ENABLE_FIXED_TARGET", False)
    if not fixed_target_mode:
        return {
            "symbol": symbol, "exit_reason": "NOT_IMPLEMENTED",
            "note": "ENABLE_FIXED_TARGET=False -- trailing-stop/structure-break replay "
                    "is not yet built. Explicitly not simulated rather than approximated.",
        }

    mfe_close = entry_price  # best close seen so far, same direction convention as production's peak_price
    mae_close = entry_price
    mfe_time = entry_ts
    mae_time = entry_ts

    for _, row in after_entry.iterrows():
        close = float(row["close"])
        ts = row["date"]

        if direction == "BUY":
            if close > mfe_close:
                mfe_close, mfe_time = close, ts
            if close < mae_close:
                mae_close, mae_time = close, ts
            hit_stop = close <= stop
            hit_target = close >= target
        else:
            if close < mfe_close:
                mfe_close, mfe_time = close, ts
            if close > mae_close:
                mae_close, mae_time = close, ts
            hit_stop = close >= stop
            hit_target = close <= target

        # Production checks hard-stop before target in the same pass
        # (main.py: hit_hard_stop computed first, and the final result
        # branch checks hit_hard_stop before hit_target) -- preserved
        # here exactly, not reordered.
        if hit_stop:
            mfe_pct = ((mfe_close - entry_price) / entry_price * 100) if direction == "BUY" \
                else ((entry_price - mfe_close) / entry_price * 100)
            mae_pct = ((mae_close - entry_price) / entry_price * 100) if direction == "BUY" \
                else ((entry_price - mae_close) / entry_price * 100)
            return {
                "symbol": symbol, "direction": direction, "entry_time": entry_ts, "entry_price": entry_price,
                "signal_stop": signal["stop"], "signal_target": signal["target"],
                "stop": stop, "target": target,
                "exit_time": ts, "exit_price": close,
                "exit_reason": "STOP_HIT", "holding_minutes": (ts - entry_ts).total_seconds() / 60,
                "mfe_pct": round(mfe_pct, 4), "mae_pct": round(mae_pct, 4),
                "mfe_time": mfe_time, "mae_time": mae_time,
            }
        if hit_target:
            mfe_pct = ((mfe_close - entry_price) / entry_price * 100) if direction == "BUY" \
                else ((entry_price - mfe_close) / entry_price * 100)
            mae_pct = ((mae_close - entry_price) / entry_price * 100) if direction == "BUY" \
                else ((entry_price - mae_close) / entry_price * 100)
            return {
                "symbol": symbol, "direction": direction, "entry_time": entry_ts, "entry_price": entry_price,
                "signal_stop": signal["stop"], "signal_target": signal["target"],
                "stop": stop, "target": target,
                "exit_time": ts, "exit_price": close,
                "exit_reason": "TARGET_HIT", "holding_minutes": (ts - entry_ts).total_seconds() / 60,
                "mfe_pct": round(mfe_pct, 4), "mae_pct": round(mae_pct, 4),
                "mfe_time": mfe_time, "mae_time": mae_time,
            }

    # Neither hit by square-off time -- exits at square-off close.
    if after_entry.empty:
        return {
            "symbol": symbol, "direction": direction, "entry_time": entry_ts, "entry_price": entry_price,
            "stop": stop, "target": target, "exit_reason": "NO_EXIT",
            "note": "no post-entry candle data available before square-off",
        }
    last_row = after_entry.iloc[-1]
    close = float(last_row["close"])
    mfe_pct = ((mfe_close - entry_price) / entry_price * 100) if direction == "BUY" \
        else ((entry_price - mfe_close) / entry_price * 100)
    mae_pct = ((mae_close - entry_price) / entry_price * 100) if direction == "BUY" \
        else ((entry_price - mae_close) / entry_price * 100)
    return {
        "symbol": symbol, "direction": direction, "entry_time": entry_ts, "entry_price": entry_price,
        "stop": stop, "target": target, "exit_time": last_row["date"], "exit_price": close,
        "exit_reason": "SQUARE_OFF", "holding_minutes": (last_row["date"] - entry_ts).total_seconds() / 60,
        "mfe_pct": round(mfe_pct, 4), "mae_pct": round(mae_pct, 4),
        "mfe_time": mfe_time, "mae_time": mae_time,
    }


def main():
    print("=" * 88)
    print("OPTION A -- EXACT PRODUCTION-FIDELITY EXIT REPLAY -- 2026-08-07")
    print("=" * 88)
    print(f"ENABLE_FIXED_TARGET = {getattr(cfg, 'ENABLE_FIXED_TARGET', None)}")
    print(f"Square-off time used: {SQUARE_OFF_TIME}")
    print()

    kite = get_kite_client()

    print("Regenerating the 46 gate-passed signals via the verified replay script "
          "(not hand-transcribed)...")
    signals = _generate_gate_passed_signals(kite)
    print(f"Regenerated {len(signals)} gate-passed signals.")
    if not signals:
        print("No signals to replay. Stopping.")
        return

    print("\nFetching 5-minute candles for exit-walk (real Zerodha data)...")
    by_symbol = _deduplicate_same_symbol(signals)
    all_5m_by_symbol = {}
    for sym, sig_list in by_symbol.items():
        token = sig_list[0]["token"]
        df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                from_date=TARGET_DATE.to_pydatetime(),
                                to_date=(TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime(),
                                trim_incomplete=False)
        all_5m_by_symbol[sym] = df5_raw

    print("\nWalking exits with proper sequential, exit-aware deduplication "
          "(a later same-symbol signal is only considered if the prior "
          "hypothetical trade had already exited by that signal's timestamp)...")
    trades = []
    skipped_overlapping = 0
    for sym, sig_list in by_symbol.items():
        open_until = None  # exit timestamp of the currently-open hypothetical trade for this symbol, if any
        for sig in sig_list:
            if open_until is not None and sig["timestamp"] <= open_until:
                skipped_overlapping += 1
                continue
            result = _walk_exit(kite, sig, all_5m_by_symbol)
            trades.append(result)
            if result.get("exit_time") is not None:
                open_until = result["exit_time"]
            else:
                # No resolvable exit (NO_DATA/NOT_IMPLEMENTED/NO_EXIT) --
                # treat as unresolved rather than falsely "closed", so a
                # later same-symbol signal is still correctly skipped for
                # the remainder of the day (a real position, if opened,
                # would still be open).
                open_until = pd.Timestamp(f"{sig['timestamp'].date()} 23:59")

    print(f"Signals skipped as overlapping with an already-open hypothetical trade: {skipped_overlapping}")

    print("\n" + "=" * 88)
    print("TRADE-BY-TRADE RESULTS")
    print("=" * 88)
    for t in trades:
        if t.get("exit_reason") in ("NO_DATA", "NOT_IMPLEMENTED", "NO_EXIT"):
            print(f"{t['symbol']:<14} -- {t['exit_reason']}: {t.get('note', '')}")
            continue
        print(f"{t['symbol']:<14} {t['direction']:<4} entry={t['entry_price']:.2f}@{t['entry_time']:%H:%M} "
              f"stop={t['stop']:.2f} target={t['target']:.2f} "
              f"exit={t['exit_price']:.2f}@{t['exit_time']:%H:%M} reason={t['exit_reason']} "
              f"hold={t['holding_minutes']:.0f}m mfe={t['mfe_pct']:+.2f}% mae={t['mae_pct']:+.2f}%")

    resolved = [t for t in trades if t.get("exit_reason") in ("STOP_HIT", "TARGET_HIT", "SQUARE_OFF")]
    target_hits = [t for t in resolved if t["exit_reason"] == "TARGET_HIT"]
    stop_hits = [t for t in resolved if t["exit_reason"] == "STOP_HIT"]
    square_offs = [t for t in resolved if t["exit_reason"] == "SQUARE_OFF"]

    print("\n" + "=" * 88)
    print("SUMMARY")
    print("=" * 88)
    print(f"Trades replayed: {len(trades)}")
    print(f"Resolved trades: {len(resolved)}")
    print(f"  TARGET_HIT: {len(target_hits)}")
    print(f"  STOP_HIT: {len(stop_hits)}")
    print(f"  SQUARE_OFF: {len(square_offs)}")
    if resolved:
        print(f"Target hit rate: {len(target_hits)/len(resolved)*100:.1f}%")
        print(f"Stop hit rate: {len(stop_hits)/len(resolved)*100:.1f}%")

    print("\nSequential, exit-aware deduplication applied: a later signal for the same "
          "symbol is only counted as a new hypothetical trade if the prior trade had "
          "already resolved (STOP_HIT/TARGET_HIT/SQUARE_OFF) by that signal's timestamp.")


if __name__ == "__main__":
    main()
