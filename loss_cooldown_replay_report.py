#!/usr/bin/env python3
"""Research-only loss-triggered cooldown replay for Breakout+Pullback candidates.

Fixed strategy for isolation:
- Breakout OR Pullback candidate universe from existing replay CSV
- Existing stored trade quantity
- Broker SL = 0.50%
- T1 = +0.50%, exit first half
- Runner stop moves to breakeven after T1
- T2 = +1.00%
- Same conservative same-candle ordering used in prior research
- Existing per-trade sim_costs reused for apples-to-apples comparison with the
  recent 22-trade SL/time replay (0m baseline should be close to that report).

Only variable:
- Cooldown after a NET LOSING executed trade, measured from that trade's
  simulated exit timestamp.
- Cooldowns tested: 0, 5, 10, 15, 20, 30, 45, 60 minutes.
- Winning trades do NOT start a cooldown.

This is research-only. It places no orders and changes no live state.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

import sweep_sl_rpt_max_position as base

TRADE_FILE = Path("runtime/proposed_logic_broker_sl_replay/trade_level.csv")
OUT = Path("runtime/loss_cooldown_replay")
OUT.mkdir(parents=True, exist_ok=True)

SL_PCT = 0.005
T1_PCT = 0.005
T2_PCT = 0.010
COOLDOWNS = [0, 5, 10, 15, 20, 30, 45, 60]


def truthy(v):
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def load_candidates():
    df = pd.read_csv(TRADE_FILE)
    df = df[df["breakout_pullback"].map(truthy)].copy()
    df["signal_ts"] = pd.to_datetime(df["signal_ts"], errors="coerce")
    df = df.dropna(subset=["signal_ts", "entry", "direction", "symbol", "qty"])
    df = df.sort_values(["signal_ts", "trade_index"], kind="stable").reset_index(drop=True)
    return df


def simulate_with_time(day, side, entry, qty):
    if day is None or day.empty or qty <= 0:
        return None

    buy = side == "BUY"
    stop = entry * (1 - SL_PCT if buy else 1 + SL_PCT)
    t1 = entry * (1 + T1_PCT if buy else 1 - T1_PCT)
    t2 = entry * (1 + T2_PCT if buy else 1 - T2_PCT)

    q1 = qty // 2
    q2 = qty - q1
    if q1 == 0:
        q1, q2 = 1, 0

    gross = 0.0
    hit_t1 = False
    exit_name = None
    exit_time = None

    for _, bar in day.iterrows():
        lo = float(bar["low"])
        hi = float(bar["high"])
        ts = pd.Timestamp(bar["timestamp"])

        if not hit_t1:
            stop_hit = lo <= stop if buy else hi >= stop
            t1_hit = hi >= t1 if buy else lo <= t1

            # Conservative: stop first if both touched in same candle.
            if stop_hit:
                gross = (stop - entry) * qty if buy else (entry - stop) * qty
                exit_name = "SL_0.500"
                exit_time = ts
                break

            if t1_hit:
                gross += (t1 - entry) * q1 if buy else (entry - t1) * q1
                hit_t1 = True
                if q2 == 0:
                    exit_name = "T1_ONLY"
                    exit_time = ts
                    break
        else:
            be_hit = lo <= entry if buy else hi >= entry
            t2_hit = hi >= t2 if buy else lo <= t2

            # Conservative: breakeven first if both touched in same candle.
            if be_hit:
                exit_name = "T1_PLUS_BE"
                exit_time = ts
                break

            if t2_hit:
                gross += (t2 - entry) * q2 if buy else (entry - t2) * q2
                exit_name = "T1_PLUS_T2"
                exit_time = ts
                break

    if exit_time is None:
        last = day.iloc[-1]
        px = float(last["close"])
        exit_time = pd.Timestamp(last["timestamp"])
        if not hit_t1:
            gross = (px - entry) * qty if buy else (entry - px) * qty
            exit_name = "EOD_NO_T1"
        else:
            if q2 > 0:
                gross += (px - entry) * q2 if buy else (entry - px) * q2
            exit_name = "T1_PLUS_EOD"

    return {
        "gross": float(gross),
        "exit": exit_name,
        "exit_time": exit_time,
        "stop": stop,
        "t1": t1,
        "t2": t2,
    }


def build_independent_outcomes(df):
    rows = []
    for _, t in df.iterrows():
        symbol = str(t["symbol"])
        side = str(t["direction"]).upper()
        entry = float(t["entry"])
        qty = int(t["qty"])
        signal_ts = pd.Timestamp(t["signal_ts"])

        day = base.load_day_candles(symbol, signal_ts)
        sim = simulate_with_time(day, side, entry, qty)
        if sim is None:
            continue

        costs = pd.to_numeric(pd.Series([t.get("sim_costs")]), errors="coerce").iloc[0]
        costs = 0.0 if pd.isna(costs) else float(costs)
        net = float(sim["gross"] - costs)

        rows.append({
            "trade_index": t.get("trade_index"),
            "date": str(t["date"]),
            "signal_ts": signal_ts,
            "symbol": symbol,
            "direction": side,
            "entry": entry,
            "qty": qty,
            "breakout": truthy(t.get("breakout")),
            "pullback": truthy(t.get("pullback")),
            "adx14": t.get("adx14"),
            "ema_distance_atr": t.get("ema_distance_atr"),
            "volume_ratio20": t.get("volume_ratio20"),
            "atr_multiple": t.get("atr_multiple"),
            "clv": t.get("clv"),
            "gross": float(sim["gross"]),
            "costs": costs,
            "net": net,
            "exit": sim["exit"],
            "exit_time": pd.Timestamp(sim["exit_time"]),
        })

    out = pd.DataFrame(rows)
    return out.sort_values(["signal_ts", "trade_index"], kind="stable").reset_index(drop=True)


def max_drawdown(values):
    eq = peak = worst = 0.0
    for v in values:
        eq += float(v)
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def run_cooldown(outcomes, minutes):
    accepted = []
    blocked = []
    cooldown_until = None
    trigger_loss = None

    for _, row in outcomes.iterrows():
        signal_ts = pd.Timestamp(row["signal_ts"])

        if minutes > 0 and cooldown_until is not None and signal_ts < cooldown_until:
            b = row.to_dict()
            b.update({
                "cooldown_min": minutes,
                "decision": "COOLDOWN_BLOCK",
                "cooldown_until": cooldown_until,
                "trigger_loss_symbol": trigger_loss["symbol"] if trigger_loss is not None else None,
                "trigger_loss_exit_time": trigger_loss["exit_time"] if trigger_loss is not None else None,
            })
            blocked.append(b)
            continue

        a = row.to_dict()
        a.update({
            "cooldown_min": minutes,
            "decision": "TAKEN",
            "cooldown_until": pd.NaT,
            "trigger_loss_symbol": None,
            "trigger_loss_exit_time": pd.NaT,
        })
        accepted.append(a)

        if float(row["net"]) <= 0 and minutes > 0:
            cooldown_until = pd.Timestamp(row["exit_time"]) + pd.Timedelta(minutes=minutes)
            trigger_loss = row

    a = pd.DataFrame(accepted)
    b = pd.DataFrame(blocked)

    wins = int((a["net"] > 0).sum()) if not a.empty else 0
    losses = int((a["net"] <= 0).sum()) if not a.empty else 0
    pos = float(a.loc[a["net"] > 0, "net"].sum()) if not a.empty else 0.0
    neg = abs(float(a.loc[a["net"] < 0, "net"].sum())) if not a.empty else 0.0

    blocked_winners = int((b["net"] > 0).sum()) if not b.empty else 0
    blocked_losers = int((b["net"] <= 0).sum()) if not b.empty else 0
    blocked_net = float(b["net"].sum()) if not b.empty else 0.0

    summary = {
        "cooldown_min": minutes,
        "candidate_trades": len(outcomes),
        "trades_taken": len(a),
        "winners": wins,
        "losers_flat": losses,
        "win_rate_pct": wins / len(a) * 100.0 if len(a) else 0.0,
        "gross_profit_net_basis": pos,
        "gross_loss_net_basis": -neg,
        "gross": float(a["gross"].sum()) if not a.empty else 0.0,
        "costs": float(a["costs"].sum()) if not a.empty else 0.0,
        "net": float(a["net"].sum()) if not a.empty else 0.0,
        "profit_factor": pos / neg if neg > 0 else None,
        "max_drawdown": max_drawdown(a["net"].tolist()) if not a.empty else 0.0,
        "sl_hits": int(a["exit"].astype(str).str.startswith("SL_").sum()) if not a.empty else 0,
        "cooldown_blocked": len(b),
        "blocked_winners": blocked_winners,
        "blocked_losers_avoided": blocked_losers,
        "blocked_counterfactual_net": blocked_net,
        "winner_profit_blocked": float(b.loc[b["net"] > 0, "net"].sum()) if not b.empty else 0.0,
        "loser_loss_avoided": abs(float(b.loc[b["net"] < 0, "net"].sum())) if not b.empty else 0.0,
    }
    return summary, a, b


def main():
    df = load_candidates()
    print(f"Breakout+Pullback candidates: {len(df)}")
    outcomes = build_independent_outcomes(df)
    print(f"Replayable independent outcomes: {len(outcomes)}")

    outcomes.to_csv(OUT / "independent_0m_outcomes.csv", index=False)

    summaries = []
    taken_frames = []
    blocked_frames = []

    for cd in COOLDOWNS:
        summary, taken, blocked = run_cooldown(outcomes, cd)
        summaries.append(summary)
        if not taken.empty:
            taken_frames.append(taken)
        if not blocked.empty:
            blocked_frames.append(blocked)

    s = pd.DataFrame(summaries)
    s.to_csv(OUT / "cooldown_summary.csv", index=False)
    if taken_frames:
        pd.concat(taken_frames, ignore_index=True).to_csv(OUT / "taken_trade_detail.csv", index=False)
    if blocked_frames:
        pd.concat(blocked_frames, ignore_index=True).to_csv(OUT / "blocked_trade_detail.csv", index=False)

    print("\n===== LOSS-TRIGGERED COOLDOWN SUMMARY =====")
    cols = [
        "cooldown_min", "trades_taken", "winners", "losers_flat", "win_rate_pct",
        "cooldown_blocked", "blocked_winners", "blocked_losers_avoided", "sl_hits",
        "gross", "costs", "net", "profit_factor", "max_drawdown",
        "winner_profit_blocked", "loser_loss_avoided", "blocked_counterfactual_net",
    ]
    print(s[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    base_net = float(s.loc[s["cooldown_min"] == 0, "net"].iloc[0])
    s["delta_vs_0m"] = s["net"] - base_net

    print("\n===== RANKED BY NET P&L =====")
    rank_cols = [
        "cooldown_min", "trades_taken", "winners", "losers_flat", "win_rate_pct",
        "cooldown_blocked", "blocked_winners", "blocked_losers_avoided",
        "net", "delta_vs_0m", "profit_factor", "max_drawdown",
    ]
    print(s.sort_values(["net", "profit_factor"], ascending=[False, False])[rank_cols].to_string(
        index=False, float_format=lambda x: f"{x:.4f}"
    ))

    # Consecutive-loss/candidate clustering independent of applying a cooldown.
    cluster_rows = []
    for _, loss in outcomes[outcomes["net"] <= 0].iterrows():
        exit_ts = pd.Timestamp(loss["exit_time"])
        later = outcomes[outcomes["signal_ts"] >= exit_ts].copy()
        for window in [5, 10, 15, 20, 30, 45, 60]:
            until = exit_ts + pd.Timedelta(minutes=window)
            q = later[later["signal_ts"] < until]
            cluster_rows.append({
                "loss_date": loss["date"],
                "loss_symbol": loss["symbol"],
                "loss_exit_time": exit_ts,
                "window_min": window,
                "next_candidates": len(q),
                "next_winners": int((q["net"] > 0).sum()),
                "next_losers": int((q["net"] <= 0).sum()),
                "next_counterfactual_net": float(q["net"].sum()) if len(q) else 0.0,
            })

    clusters = pd.DataFrame(cluster_rows)
    clusters.to_csv(OUT / "post_loss_candidate_clustering.csv", index=False)

    print("\n===== POST-LOSS CANDIDATE CLUSTERING =====")
    agg = clusters.groupby("window_min").agg(
        loss_events=("loss_symbol", "count"),
        next_candidates=("next_candidates", "sum"),
        next_winners=("next_winners", "sum"),
        next_losers=("next_losers", "sum"),
        counterfactual_net=("next_counterfactual_net", "sum"),
    ).reset_index()
    print(agg.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Show exactly what the best cooldown blocks.
    best_cd = int(s.sort_values(["net", "profit_factor"], ascending=[False, False]).iloc[0]["cooldown_min"])
    _, _, best_blocked = run_cooldown(outcomes, best_cd)
    print(f"\n===== BEST COOLDOWN BLOCKED TRADE DETAIL ({best_cd} MIN) =====")
    if best_blocked.empty:
        print("None")
    else:
        detail_cols = [
            "date", "signal_ts", "symbol", "direction", "entry", "exit", "exit_time",
            "net", "trigger_loss_symbol", "trigger_loss_exit_time", "cooldown_until",
        ]
        print(best_blocked[detail_cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
