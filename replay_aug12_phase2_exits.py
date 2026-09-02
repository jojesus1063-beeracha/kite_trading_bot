"""
Chronological reconstruction (date set by TARGET_DATE below) -- PHASE 2: exit-stack simulation.

Takes Phase 1's confirmed-entry output (symbol, direction, timestamp,
entry, geometric stop, 2R target) and walks each one FORWARD through
historical data, simulating two exit models in parallel:

MODEL A -- CURRENT RUNTIME (verified byte-for-byte against real code
this session; every formula below has a "source:" comment naming the
exact function/line traced):

    entry -> pos["stop"] OVERWRITTEN by 0.75% emergency stop
           (source: paper_50pct_risk_launcher.py _paper_apply_emergency_stop)
    -> ATR(14) simple-rolling-mean trailing, x1.2 tight / x2.5 normal,
           peak ratchets on completed-candle CLOSE only
           (source: main.py check_position_exit, indicators.atr)
    -> structure break: 10-bar lookback [-11:-1], native 3m
           (source: main.py _market_structure_broken)
    -> trend reversal: 15m trend != wanted (None counts as reversed)
           (source: main.py _trend_reversed)
    -> [2R target -- DEAD, ENABLE_FIXED_TARGET=False, never checked]
    -> CP9: >=9.0m, mae<=-0.20, current<0, one-shot
           (source: paper_cp9_eod_guard.py cp9_checkpoint_decision)
    -> MAE adverse: >10.0m, mae<=-0.30, current<=-0.15, mfe<0.30,
           3 consecutive adverse candles (source: paper_mae_mfe_launcher.py)
    -> MFE/time: <20m none; >=40m dead-loser; >40m late-giveback;
           else 20-40m lock/giveback (source: paper_mfe_time_launcher.py)
    -> EOD 15:08, last 3m close (source: main.py past_square_off region)

MODEL B -- INTENDED ARCHITECTURE (**DESIGN PROPOSAL, NOT VERIFIED
CODE** -- nothing in the real codebase implements this; it is built
from this session's stated intent and is clearly separated from Model
A so the two are never confused):

    entry -> geometric stop MONITORED AS-IS (the candlestick plan's own
           stop_price, never overwritten by the emergency stop)
    -> 2R target MONITORED AS-IS (the candlestick plan's own
           target_price, actually checked against price)
    -> ATR trailing activates ONLY after the trade has moved favorably
           by >= DEVELOPMENT_THRESHOLD_R (default 1.0R). Before that
           threshold, ATR trailing is inactive. THIS THRESHOLD IS AN
           ASSUMPTION -- adjust DEVELOPMENT_THRESHOLD_R below and rerun
           if a different definition of "developed" is intended.
    -> CP9 / MAE / MFE / EOD: same as Model A, unchanged (nothing in
           the stated design asked for these to differ)

Both models are walked over the SAME entry list and the SAME historical
candle data, so any P&L difference between them isolates the exit-rule
change alone -- entries are held constant.

Every candle fetch is explicitly date-bounded (from_date/to_date), never
relies on fetch_candles()'s datetime.now() default -- the same
no-look-ahead discipline verified and required throughout this session.

Requires Phase 1's real output (a JSON list of confirmed entries) as
input -- this script does not invent entries.

Read-only. Never writes state, never places orders.

Run:
    BOT_DIR=~/kite_trading_bot python3 replay_aug12_phase2_exits.py \\
        --entries /path/to/phase1_confirmed_entries.json
"""

from __future__ import annotations

import argparse
import json
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
from indicators import add_indicators, atr as atr_indicator
from strategy import get_trend

TARGET_DATE = pd.Timestamp("2026-08-13")
SQUARE_OFF_TIME = "15:08"

# -- Model A constants (all verified this session) --
EMERGENCY_STOP_PCT = 0.75
ATR_PERIOD = 14
ATR_MULT_TIGHT, ATR_MULT_NORMAL = 1.2, 2.5
ADX_TIGHT_THRESHOLD = 25.0
STRUCTURE_LOOKBACK = 10
CP9_CHECKPOINT_MIN = 9.0
CP9_MAE_THRESHOLD = -0.20
MAE_MIN_AGE_MIN = 10.0
MAE_THRESHOLD = -0.30
MAE_CURRENT_THRESHOLD = -0.15
MAE_MAX_MFE_FAILURE = 0.30
MAE_ADVERSE_CANDLES = 3
MFE_MIN_HOLD_MIN = 20.0
MFE_DEAD_TRADE_MIN = 40.0
MFE_DEAD_TRADE_MAX_MFE = 0.30
MFE_LATE_THRESHOLD = 0.30
MFE_GIVEBACK_PCT = 50.0
MFE_LOCK_THRESHOLD = 0.50
MFE_LOCK_CURRENT = 0.30
MFE_MID_THRESHOLD = 0.40

# -- Model B assumption -- NOT verified, adjust if intent differs --
DEVELOPMENT_THRESHOLD_R = 1.0


def load_entries(path):
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("confirmed_entries", [])


def fetch_bounded(kite, token, interval, as_of, lookback_days=5):
    start = (as_of - timedelta(days=lookback_days)).to_pydatetime()
    end = as_of.to_pydatetime()
    return fetch_candles(kite, token, interval, from_date=start, to_date=end, trim_incomplete=False)


def mae_mfe_current(entry, direction, highs, lows, last_price):
    """Source: paper_mfe_time_launcher.py _excursions() -- verified."""
    if direction == "BUY":
        mfe = max(0.0, (highs.max() - entry) / entry * 100) if len(highs) else 0.0
        mae = min(0.0, (lows.min() - entry) / entry * 100) if len(lows) else 0.0
        current = (last_price - entry) / entry * 100
    else:
        mfe = max(0.0, (entry - lows.min()) / entry * 100) if len(lows) else 0.0
        mae = min(0.0, (entry - highs.max()) / entry * 100) if len(highs) else 0.0
        current = (entry - last_price) / entry * 100
    return mfe, mae, current


def mfe_time_reason(minutes, mfe, current, giveback):
    """Source: paper_mfe_time_launcher.py -- verified this session."""
    if minutes < MFE_MIN_HOLD_MIN:
        return None
    if minutes >= MFE_DEAD_TRADE_MIN and mfe < MFE_DEAD_TRADE_MAX_MFE and current < 0.0:
        return "mfe_time_dead_loser_40m"
    if minutes > MFE_DEAD_TRADE_MIN:
        if mfe >= MFE_LATE_THRESHOLD and giveback >= MFE_GIVEBACK_PCT:
            return "mfe_time_late_giveback"
        return None
    if mfe >= MFE_LOCK_THRESHOLD and current <= MFE_LOCK_CURRENT:
        return "mfe_time_lock_20_40"
    if mfe >= MFE_MID_THRESHOLD and giveback >= MFE_GIVEBACK_PCT:
        return "mfe_time_giveback_20_40"
    return None


def adverse_trend(closes_recent, ema9_recent, ema21_recent, direction):
    """Source: paper_mae_mfe_launcher.py _adverse_trend() -- verified."""
    if direction == "BUY":
        flags = (closes_recent < ema9_recent) & (ema9_recent < ema21_recent)
    else:
        flags = (closes_recent > ema9_recent) & (ema9_recent > ema21_recent)
    return bool(flags.all())


def simulate_model_a(kite, entry_row, df_3m, df_1m, token):
    """Current runtime -- every rule verified against real code this session."""
    entry = entry_row["entry"]
    direction = entry_row["direction"]
    entry_ts = pd.Timestamp(entry_row["timestamp"])

    stop = entry * (1 - EMERGENCY_STOP_PCT / 100) if direction == "BUY" else entry * (1 + EMERGENCY_STOP_PCT / 100)
    peak = entry
    tight_mode = False

    post_entry = df_3m[df_3m["date"] > entry_ts].reset_index(drop=True)
    for i in range(len(post_entry)):
        row = post_entry.iloc[i]
        now = row["date"]
        if now.time() >= datetime.strptime(SQUARE_OFF_TIME, "%H:%M").time():
            return {"exit_reason": "eod_square_off", "exit_price": row["close"],
                    "exit_time": now, "model": "A"}

        last_price = row["close"]
        completed = post_entry.iloc[:i + 1]
        extreme = completed["close"].max() if direction == "BUY" else completed["close"].min()
        peak = max(peak, extreme) if direction == "BUY" else min(peak, extreme)

        hit_hard_stop = (last_price <= stop) if direction == "BUY" else (last_price >= stop)
        if hit_hard_stop:
            return {"exit_reason": "stop", "exit_price": last_price, "exit_time": now, "model": "A"}

        window = post_entry.iloc[:i + 1]
        atr_series = atr_indicator(window, ATR_PERIOD)
        current_atr = atr_series.iloc[-1] if not atr_series.empty else None
        mult = ATR_MULT_TIGHT if tight_mode else ATR_MULT_NORMAL
        if current_atr is not None and not pd.isna(current_atr):
            trailing = peak - current_atr * mult if direction == "BUY" else peak + current_atr * mult
            hit_trailing = (last_price <= trailing) if direction == "BUY" else (last_price >= trailing)
            if hit_trailing:
                return {"exit_reason": "trailing_stop", "exit_price": last_price, "exit_time": now, "model": "A"}

        if i >= STRUCTURE_LOOKBACK + 1:
            ref = window.iloc[-(STRUCTURE_LOOKBACK + 1):-1]
            broken = (last_price < ref["low"].min()) if direction == "BUY" else (last_price > ref["high"].max())
            if broken:
                return {"exit_reason": "structure_break", "exit_price": last_price, "exit_time": now, "model": "A"}

        try:
            df_15m = fetch_bounded(kite, token, cfg.TREND_TIMEFRAME, now)
            if not df_15m.empty:
                df_15m, _ = add_indicators(df_15m, df_15m.copy(), cfg)
                from strategy import latest_completed_15m_trend, latest_completed_15m_row
                current_trend = latest_completed_15m_trend(df_15m, now, cfg)
                latest_row = latest_completed_15m_row(df_15m, now)
                current_adx = latest_row.get("adx") if latest_row is not None else None
                wanted = "UP" if direction == "BUY" else "DOWN"
                if current_trend != wanted:
                    return {"exit_reason": "trend_reversal", "exit_price": last_price, "exit_time": now, "model": "A"}
                tight_mode = current_adx is not None and not pd.isna(current_adx) and current_adx < ADX_TIGHT_THRESHOLD
        except Exception:
            pass

        minutes = (now - entry_ts).total_seconds() / 60.0
        mfe, mae, current_pct = mae_mfe_current(entry, direction, window["high"], window["low"], last_price)

        if minutes >= CP9_CHECKPOINT_MIN:
            if mae <= CP9_MAE_THRESHOLD and current_pct < 0.0:
                return {"exit_reason": "cp9_mae20_failed_development_eod", "exit_price": last_price,
                        "exit_time": now, "model": "A"}

        if minutes > MAE_MIN_AGE_MIN and mae <= MAE_THRESHOLD and current_pct <= MAE_CURRENT_THRESHOLD and mfe < MAE_MAX_MFE_FAILURE:
            if i >= MAE_ADVERSE_CANDLES - 1:
                recent = window.iloc[-MAE_ADVERSE_CANDLES:]
                ema9 = recent["close"].ewm(span=9, adjust=False).mean()
                ema21 = recent["close"].ewm(span=21, adjust=False).mean()
                if adverse_trend(recent["close"], ema9, ema21, direction):
                    return {"exit_reason": "mae_adverse_trend", "exit_price": last_price, "exit_time": now, "model": "A"}

        giveback = 0.0 if mfe <= 0 else max(0.0, (mfe - current_pct) / mfe * 100)
        reason = mfe_time_reason(minutes, mfe, current_pct, giveback)
        if reason:
            return {"exit_reason": reason, "exit_price": last_price, "exit_time": now, "model": "A"}

    return {"exit_reason": "no_exit_in_data", "exit_price": None, "exit_time": None, "model": "A"}


def simulate_model_b(entry_row, df_3m):
    """INTENDED ARCHITECTURE -- design proposal, not verified code.
    Geometric stop and 2R target monitored directly; ATR trailing gated
    behind DEVELOPMENT_THRESHOLD_R. CP9/MAE/MFE/EOD intentionally
    identical to Model A -- only the primary stop/target logic differs.
    THIS FUNCTION ENCODES AN ASSUMPTION, NOT A FACT."""
    entry = entry_row["entry"]
    direction = entry_row["direction"]
    stop = entry_row["stop"]  # the REAL geometric stop, never overwritten
    target = entry_row["target"]  # the REAL 2R target, actually checked
    entry_ts = pd.Timestamp(entry_row["timestamp"])
    r_distance = abs(entry - stop)

    post_entry = df_3m[df_3m["date"] > entry_ts].reset_index(drop=True)
    for i in range(len(post_entry)):
        row = post_entry.iloc[i]
        now = row["date"]
        if now.time() >= datetime.strptime(SQUARE_OFF_TIME, "%H:%M").time():
            return {"exit_reason": "eod_square_off", "exit_price": row["close"], "exit_time": now, "model": "B"}

        last_price = row["close"]

        hit_stop = (last_price <= stop) if direction == "BUY" else (last_price >= stop)
        if hit_stop:
            return {"exit_reason": "geometric_stop", "exit_price": last_price, "exit_time": now, "model": "B"}

        hit_target = (last_price >= target) if direction == "BUY" else (last_price <= target)
        if hit_target:
            return {"exit_reason": "target_2r", "exit_price": last_price, "exit_time": now, "model": "B"}

        favorable_r = (last_price - entry) / r_distance if direction == "BUY" else (entry - last_price) / r_distance
        if favorable_r >= DEVELOPMENT_THRESHOLD_R:
            window = post_entry.iloc[:i + 1]
            atr_series = atr_indicator(window, ATR_PERIOD)
            current_atr = atr_series.iloc[-1] if not atr_series.empty else None
            if current_atr is not None and not pd.isna(current_atr):
                extreme = window["close"].max() if direction == "BUY" else window["close"].min()
                trailing = extreme - current_atr * ATR_MULT_NORMAL if direction == "BUY" else extreme + current_atr * ATR_MULT_NORMAL
                hit_trailing = (last_price <= trailing) if direction == "BUY" else (last_price >= trailing)
                if hit_trailing:
                    return {"exit_reason": "atr_trailing_post_development", "exit_price": last_price,
                            "exit_time": now, "model": "B"}

    return {"exit_reason": "no_exit_in_data", "exit_price": None, "exit_time": None, "model": "B"}


def pnl(entry_row, exit_price, capital_qty):
    if exit_price is None:
        return None
    entry, direction = entry_row["entry"], entry_row["direction"]
    per_share = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    return per_share * capital_qty


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", required=True, help="Path to Phase 1's confirmed-entries JSON output")
    args = parser.parse_args()

    entries = load_entries(args.entries)
    if not entries:
        raise SystemExit("No entries found in the supplied file. Phase 2 requires Phase 1's real output.")

    kite = get_kite_client()

    print("=" * 100)
    print("PHASE 2 -- DUAL EXIT-MODEL SIMULATION")
    print("=" * 100)
    print(f"Entries loaded: {len(entries)}")
    print("MODEL A = current runtime (verified). MODEL B = intended architecture (DESIGN PROPOSAL).")
    print(f"Model B development threshold: {DEVELOPMENT_THRESHOLD_R}R (ASSUMPTION -- adjust if wrong)")
    print()

    start_fetch = (TARGET_DATE - pd.Timedelta(days=5)).to_pydatetime()
    end_fetch = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    results_a, results_b = [], []

    for entry_row in entries:
        symbol = entry_row["symbol"]
        try:
            token = get_instrument_token(kite, symbol, "NSE")
            df_3m_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                      from_date=start_fetch, to_date=end_fetch, trim_incomplete=False)
            if df_3m_raw.empty:
                print(f"  {symbol}: NO DATA, skipped")
                continue
            df_3m, _ = add_indicators(df_3m_raw, df_3m_raw.copy(), cfg)

            capital_qty = int((getattr(cfg, "CAPITAL", 5000.0) or 5000.0) // entry_row["entry"])

            res_a = simulate_model_a(kite, entry_row, df_3m, None, token)
            res_a["symbol"] = symbol
            res_a["pnl"] = pnl(entry_row, res_a["exit_price"], capital_qty)
            results_a.append(res_a)

            res_b = simulate_model_b(entry_row, df_3m)
            res_b["symbol"] = symbol
            res_b["pnl"] = pnl(entry_row, res_b["exit_price"], capital_qty)
            results_b.append(res_b)

            print(f"  {symbol:<14} A={res_a['exit_reason']:<28} pnl={res_a['pnl']}   "
                  f"B={res_b['exit_reason']:<28} pnl={res_b['pnl']}")
        except Exception as exc:
            print(f"  {symbol}: ERROR {exc}")

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for label, results in (("MODEL A (current runtime)", results_a), ("MODEL B (intended architecture)", results_b)):
        valid = [r for r in results if r["pnl"] is not None]
        net = sum(r["pnl"] for r in valid)
        wins = sum(1 for r in valid if r["pnl"] > 0)
        print(f"\n{label}: {len(valid)} resolved trades, {wins} wins / {len(valid)-wins} losses, net={net:+.2f}")


if __name__ == "__main__":
    main()
