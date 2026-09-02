#!/usr/bin/env python3
"""Replay proposed 3-path entry logic with a 0.5% broker-side stop.

Research only. Does not touch live trading state.

Uses the already-downloaded historical candles and matching helpers from
replay_current_entry_filters_253.py. It compares:
  CURRENT_CORE     - current strict breakout core gates
  BREAKOUT_PULLBACK- current breakout OR trend-pullback path
  PROPOSED_3PATH   - breakout OR trend-pullback OR rejection/continuation

Exit model is deliberately explicit and standardized for comparison:
  * initial broker stop = 0.5% from entry
  * 1R target = +0.5% favourable move
  * 2R target = +1.0% favourable move
  * half exits at 1R, runner stop moves to breakeven
  * runner exits at 2R, breakeven, or end-of-day close
  * if stop and target are both touched in the same 3-minute candle before 1R,
    stop is assumed first (conservative)
  * after 1R, if breakeven and 2R are both touched in one candle, breakeven is
    assumed first (conservative)

P&L uses historical trade quantity and the repo's equity intraday cost model.
This isolates entry/exit logic. It is NOT a full recreation of live position
sizing or tick-level order sequencing.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

import replay_current_entry_filters_253 as base
from costs import estimate_trade_cost

OUT = Path("runtime/proposed_logic_broker_sl_replay")
OUT.mkdir(parents=True, exist_ok=True)
SL_PCT = 0.005
T1_PCT = 0.005
T2_PCT = 0.010


def fnum(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def local_day(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Kolkata")
    else:
        t = t.tz_convert("Asia/Kolkata")
    return t.date()


def candle_features(row, direction):
    o, h, l, c = [fnum(row.get(k)) for k in ("open", "high", "low", "close")]
    atr = fnum(row.get("atr14"))
    ema9 = fnum(row.get("ema9"))
    vwap = fnum(row.get("vwap"))
    clv = fnum(row.get("clv"))
    if None in (o, h, l, c, atr) or atr <= 0:
        return {}
    body = abs(c - o)
    lower_wick = max(0.0, min(o, c) - l)
    upper_wick = max(0.0, h - max(o, c))
    near_ema = ema9 is not None and abs(c - ema9) / atr <= 0.75
    near_vwap = vwap is not None and abs(c - vwap) / atr <= 0.75
    buy = direction == "BUY"
    resumption = (c > o and (ema9 is None or c >= ema9)) if buy else (c < o and (ema9 is None or c <= ema9))
    wick_reject = ((lower_wick >= max(body, 0.10 * atr)) and clv is not None and clv >= 0.30) if buy else ((upper_wick >= max(body, 0.10 * atr)) and clv is not None and clv <= -0.30)
    return {
        "body": body,
        "lower_wick": lower_wick,
        "upper_wick": upper_wick,
        "near_ema": bool(near_ema),
        "near_vwap": bool(near_vwap),
        "resumption": bool(resumption),
        "wick_reject": bool(wick_reject),
    }


def classify_paths(ev, row, direction):
    cf = candle_features(row, direction)
    adx_ok = bool(ev.get("adx_pass"))
    dir_ok = bool(ev.get("direction_pass"))
    eda = fnum(ev.get("ema_distance_atr"))
    base_quality = adx_ok and eda is not None and eda <= 2.0

    breakout = bool(ev.get("core_pass"))

    pullback = (
        base_quality
        and dir_ok
        and eda <= 1.5
        and (cf.get("near_ema") or cf.get("near_vwap"))
        and cf.get("resumption", False)
    )

    rejection = (
        base_quality
        and cf.get("wick_reject", False)
    )

    return {
        "breakout": bool(breakout),
        "pullback": bool(pullback),
        "rejection": bool(rejection),
        "breakout_pullback": bool(breakout or pullback),
        "proposed_3path": bool(breakout or pullback or rejection),
        **cf,
    }


def path_after_entry(df, signal_ts, trade_date):
    if pd.isna(signal_ts):
        return df.iloc[0:0]
    ts = pd.Timestamp(signal_ts)
    candle_tz = df["timestamp"].dt.tz
    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert(candle_tz)
    day = pd.to_datetime(trade_date).date()
    dates = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date if candle_tz is not None else df["timestamp"].dt.date
    # Start strictly after signal timestamp: signal timestamp is treated as a completed candle.
    return df[(df["timestamp"] > ts) & (dates == day)].copy()


def gross_and_cost(direction, qty, entry, exits):
    # exits = [(qty_fraction, price), ...], fractions sum to 1.
    gross = 0.0
    exit_value = 0.0
    for frac, px in exits:
        q = qty * frac
        exit_value += q * px
        gross += ((px - entry) if direction == "BUY" else (entry - px)) * q
    entry_value = qty * entry
    if direction == "BUY":
        buy_value, sell_value = entry_value, exit_value
    else:
        sell_value, buy_value = entry_value, exit_value
    costs = estimate_trade_cost(buy_value, sell_value)
    return gross, costs, gross - costs


def simulate_trade(df, signal_ts, trade, direction):
    entry = fnum(trade.get("entry"))
    qty = fnum(trade.get("qty"), 0.0) or 0.0
    if entry is None or qty <= 0:
        return {"sim_status": "BAD_ENTRY_OR_QTY"}
    path = path_after_entry(df, signal_ts, trade.get("date"))
    if path.empty:
        return {"sim_status": "NO_POST_ENTRY_CANDLES"}

    buy = direction == "BUY"
    stop = entry * (1 - SL_PCT if buy else 1 + SL_PCT)
    t1 = entry * (1 + T1_PCT if buy else 1 - T1_PCT)
    t2 = entry * (1 + T2_PCT if buy else 1 - T2_PCT)

    stage = "PRE_T1"
    t1_time = None
    for _, r in path.iterrows():
        lo, hi = float(r["low"]), float(r["high"])
        ts = r["timestamp"]
        stop_hit = lo <= stop if buy else hi >= stop
        t1_hit = hi >= t1 if buy else lo <= t1
        t2_hit = hi >= t2 if buy else lo <= t2
        be_hit = lo <= entry if buy else hi >= entry

        if stage == "PRE_T1":
            # Conservative same-candle ambiguity: stop wins.
            if stop_hit:
                gross, costs, net = gross_and_cost(direction, qty, entry, [(1.0, stop)])
                return {"sim_status":"OK", "sim_exit":"SL_0.5", "sim_exit_time":str(ts), "sim_gross":gross, "sim_costs":costs, "sim_net":net, "stop_price":stop, "t1":t1, "t2":t2}
            if t1_hit:
                stage = "POST_T1"
                t1_time = ts
                continue
        else:
            # Conservative same-candle ambiguity after moving stop to breakeven.
            if be_hit:
                gross, costs, net = gross_and_cost(direction, qty, entry, [(0.5, t1), (0.5, entry)])
                return {"sim_status":"OK", "sim_exit":"T1_PLUS_BE", "sim_exit_time":str(ts), "sim_gross":gross, "sim_costs":costs, "sim_net":net, "stop_price":stop, "t1":t1, "t2":t2, "t1_time":str(t1_time)}
            if t2_hit:
                gross, costs, net = gross_and_cost(direction, qty, entry, [(0.5, t1), (0.5, t2)])
                return {"sim_status":"OK", "sim_exit":"T1_PLUS_T2", "sim_exit_time":str(ts), "sim_gross":gross, "sim_costs":costs, "sim_net":net, "stop_price":stop, "t1":t1, "t2":t2, "t1_time":str(t1_time)}

    eod = float(path.iloc[-1]["close"])
    if stage == "PRE_T1":
        exits = [(1.0, eod)]
        label = "EOD_NO_T1"
    else:
        exits = [(0.5, t1), (0.5, eod)]
        label = "T1_PLUS_EOD"
    gross, costs, net = gross_and_cost(direction, qty, entry, exits)
    return {"sim_status":"OK", "sim_exit":label, "sim_exit_time":str(path.iloc[-1]["timestamp"]), "sim_gross":gross, "sim_costs":costs, "sim_net":net, "stop_price":stop, "t1":t1, "t2":t2, "t1_time":str(t1_time) if t1_time is not None else None}


def main():
    trades = base.load_trades()
    signals_by_date = base.load_signals_by_date()
    candle_cache = {}
    out = []

    for i, trade in enumerate(trades, 1):
        d = str(trade.get("date") or "")
        symbol = str(trade.get("symbol") or "")
        direction = str(trade.get("direction") or "").upper()
        hist_pnl = fnum(trade.get("pnl"), 0.0) or 0.0
        base_row = {"trade_index":i,"date":d,"symbol":symbol,"direction":direction,"entry":fnum(trade.get("entry")),"qty":fnum(trade.get("qty"),0.0),"historical_net":hist_pnl,"historical_winner":hist_pnl>0}

        signal, quality = base.match_trade_to_signal(trade, signals_by_date.get(d, []))
        if signal is None:
            out.append({**base_row,"status":"NO_SIGNAL_MATCH"})
            continue
        if symbol not in candle_cache:
            c = base.load_candles(symbol)
            candle_cache[symbol] = base.add_indicators(c) if c is not None else None
        df = candle_cache[symbol]
        if df is None:
            out.append({**base_row,"status":"NO_CANDLES"})
            continue
        sts = base.signal_timestamp(signal)
        row = base.row_at_or_before(df, sts, d)
        if row is None:
            out.append({**base_row,"status":"NO_CANDLE_AT_SIGNAL"})
            continue
        ev = base.evaluate_gates(row, direction, signal, trade)
        paths = classify_paths(ev, row, direction)
        sim = simulate_trade(df, sts, trade, direction)
        out.append({**base_row,"status":"OK","signal_match_quality":quality,"signal_ts":str(sts),**ev,**paths,**sim})

    df = pd.DataFrame(out)
    df.to_csv(OUT / "trade_level.csv", index=False)

    scenarios = {
        "CURRENT_CORE":"core_pass",
        "BREAKOUT_OR_PULLBACK":"breakout_pullback",
        "PROPOSED_3PATH":"proposed_3path",
    }
    summary = {"historical_trade_count":len(trades),"replay_status_counts":df["status"].value_counts(dropna=False).to_dict(),"exit_model":{"broker_sl_pct":SL_PCT,"t1_pct":T1_PCT,"t2_pct":T2_PCT,"runner_stop_after_t1":"breakeven","same_candle_ambiguity":"stop/breakeven first (conservative)"},"scenarios":{}}

    ok = df[(df["status"]=="OK") & (df["sim_status"]=="OK")].copy()
    for name, col in scenarios.items():
        a = ok[ok[col] == True].copy()
        wins_hist = a[a["historical_winner"] == True]
        losses_hist = a[a["historical_winner"] == False]
        sim_wins = a[a["sim_net"] > 0]
        sim_losses = a[a["sim_net"] <= 0]
        gross_profit = float(sim_wins["sim_net"].sum()) if len(sim_wins) else 0.0
        gross_loss = float(sim_losses["sim_net"].sum()) if len(sim_losses) else 0.0
        pf = gross_profit / abs(gross_loss) if gross_loss < 0 else None
        summary["scenarios"][name] = {
            "accepted_trades":int(len(a)),
            "historical_winners_allowed":int(len(wins_hist)),
            "historical_losers_nonwinners_allowed":int(len(losses_hist)),
            "historical_winner_capture_pct":float(len(wins_hist) / max(1, int(ok["historical_winner"].sum())) * 100),
            "historical_loser_allow_pct":float(len(losses_hist) / max(1, int((~ok["historical_winner"]).sum())) * 100),
            "simulated_profitable":int(len(sim_wins)),
            "simulated_losing_or_flat":int(len(sim_losses)),
            "simulated_win_rate_pct":float(len(sim_wins)/len(a)*100) if len(a) else 0.0,
            "simulated_gross_pnl":float(a["sim_gross"].sum()) if len(a) else 0.0,
            "simulated_costs":float(a["sim_costs"].sum()) if len(a) else 0.0,
            "simulated_net_pnl":float(a["sim_net"].sum()) if len(a) else 0.0,
            "simulated_profit_factor":pf,
            "simulated_avg_net":float(a["sim_net"].mean()) if len(a) else 0.0,
            "simulated_worst_trade":float(a["sim_net"].min()) if len(a) else 0.0,
            "simulated_best_trade":float(a["sim_net"].max()) if len(a) else 0.0,
            "exit_counts":a["sim_exit"].value_counts().to_dict() if len(a) else {},
        }

    # Path overlap/standalone diagnostics.
    summary["path_counts"] = {
        "breakout":int(ok["breakout"].sum()),
        "pullback":int(ok["pullback"].sum()),
        "rejection":int(ok["rejection"].sum()),
        "breakout_or_pullback":int(ok["breakout_pullback"].sum()),
        "proposed_3path":int(ok["proposed_3path"].sum()),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    daily = []
    for name, col in scenarios.items():
        a = ok[ok[col] == True]
        if a.empty:
            continue
        g = a.groupby("date").agg(trades=("symbol","count"),net=("sim_net","sum"),wins=("sim_net",lambda s:int((s>0).sum())),losses=("sim_net",lambda s:int((s<=0).sum()))).reset_index()
        g.insert(0,"scenario",name)
        daily.append(g)
    if daily:
        pd.concat(daily,ignore_index=True).to_csv(OUT / "daily.csv",index=False)

    print("===== PROPOSED LOGIC + 0.5% BROKER SL REPLAY =====")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {OUT / 'trade_level.csv'}")
    print(f"Wrote {OUT / 'daily.csv'}")
    print(f"Wrote {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
