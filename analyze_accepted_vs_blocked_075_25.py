#!/usr/bin/env python3
"""Research-only analysis of the standout replay setting.

Exact configuration:
  Entry family: Breakout OR Pullback
  SL: 0.40% price
  RPT: 0.75%
  MAX_POSITION_SIZE_PCT: 25%
  MAX_DAILY_LOSS_PCT: 0.50%
  CAPITAL / AVAILABLE_MARGIN: 5000
  Current all-or-nothing daily-risk rejection behavior

Purpose:
  Reconstruct which of the 22 Breakout+Pullback candidates were accepted vs
  blocked by the current daily-risk logic, then compare their historical and
  replay characteristics so we can tell whether the risk gate is acting as a
  meaningful quality filter or merely creating a selection artifact.

Research only. Never places orders or changes live state.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

from auth import get_kite_client
from costs import net_pnl_for_trade

TRADE_FILE = Path("runtime/proposed_logic_broker_sl_replay/trade_level.csv")
CANDLE_DIR = Path("runtime/trade_replay_history/candles_3minute")
OUT = Path("runtime/accepted_vs_blocked_075_25")
OUT.mkdir(parents=True, exist_ok=True)

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0
SL_PCT = 0.004
RPT_PCT = 0.75
MAX_POSITION_PCT = 25.0
MAX_DAILY_LOSS_PCT = 0.50
MAX_TRADES_PER_DAY = 5
T1_PCT = 0.005
T2_PCT = 0.010


def truthy(v):
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def load_candidates():
    df = pd.read_csv(TRADE_FILE)
    df = df[df["breakout_pullback"].map(truthy)].copy()
    df["signal_ts"] = pd.to_datetime(df["signal_ts"], errors="coerce")
    df = df.dropna(subset=["signal_ts", "entry", "direction", "symbol"])
    return df.sort_values(["date", "signal_ts", "trade_index"]).reset_index(drop=True)


def current_margin_per_share(kite, symbols):
    out = {}
    errors = {}
    for symbol in sorted(set(symbols)):
        params = [{
            "exchange": "NSE",
            "tradingsymbol": symbol,
            "transaction_type": kite.TRANSACTION_TYPE_BUY,
            "variety": "regular",
            "product": "MIS",
            "order_type": "MARKET",
            "quantity": 1,
            "price": 0,
            "trigger_price": 0,
        }]
        try:
            r = kite.order_margins(params)
            total = float(r[0].get("total"))
            if total <= 0:
                raise ValueError(f"non-positive margin {total}")
            out[symbol] = total
        except Exception as exc:
            errors[symbol] = str(exc)
    return out, errors


def load_day(symbol, ts):
    p = CANDLE_DIR / f"{symbol.replace('/', '_')}.parquet"
    if not p.exists():
        return None
    c = pd.read_parquet(p)
    c["timestamp"] = pd.to_datetime(c["timestamp"], errors="coerce")
    c = c.dropna(subset=["timestamp"])
    tz = c["timestamp"].dt.tz
    if tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert(tz)
    day = c[(c["timestamp"].dt.date == ts.date()) & (c["timestamp"] >= ts)].copy()
    return day.sort_values("timestamp")


def simulate(day, side, entry, qty):
    if qty <= 0 or day is None or day.empty:
        return None
    buy = side == "BUY"
    stop = entry * (1 - SL_PCT if buy else 1 + SL_PCT)
    t1 = entry * (1 + T1_PCT if buy else 1 - T1_PCT)
    t2 = entry * (1 + T2_PCT if buy else 1 - T2_PCT)

    q1 = qty // 2
    q2 = qty - q1
    if q1 == 0:
        q1, q2 = 1, 0

    exits = []
    hit_t1 = False
    exit_name = None
    for _, bar in day.iterrows():
        lo, hi = float(bar["low"]), float(bar["high"])
        if not hit_t1:
            stop_hit = lo <= stop if buy else hi >= stop
            t1_hit = hi >= t1 if buy else lo <= t1
            if stop_hit:  # conservative same-candle ordering
                exits.append((qty, stop))
                exit_name = "SL_0.400"
                break
            if t1_hit:
                exits.append((q1, t1))
                hit_t1 = True
                if q2 == 0:
                    exit_name = "T1_FULL"
                    break
        else:
            be_hit = lo <= entry if buy else hi >= entry
            t2_hit = hi >= t2 if buy else lo <= t2
            if be_hit:
                exits.append((q2, entry))
                exit_name = "T1_PLUS_BE"
                break
            if t2_hit:
                exits.append((q2, t2))
                exit_name = "T1_PLUS_T2"
                break

    used = sum(q for q, _ in exits)
    if used < qty:
        last = float(day.iloc[-1]["close"])
        exits.append((qty-used, last))
        exit_name = "T1_PLUS_EOD" if hit_t1 else "EOD_NO_T1"

    gross = 0.0
    costs = 0.0
    for q, px in exits:
        if q <= 0:
            continue
        r = net_pnl_for_trade(side, int(q), float(entry), float(px))
        gross += float(r["gross_pnl"])
        costs += float(r["costs"])
    return {"sim_exit_rebuilt": exit_name, "sim_gross_rebuilt": gross,
            "sim_costs_rebuilt": costs, "sim_net_rebuilt": gross-costs}


def main():
    df = load_candidates()
    print(f"Breakout+Pullback candidates: {len(df)}")

    kite = get_kite_client()
    margin_map, errors = current_margin_per_share(kite, df["symbol"].tolist())
    print(f"Current MIS margin/share resolved: {len(margin_map)}/{df['symbol'].nunique()}")
    if errors:
        print("Margin errors:")
        for s, e in sorted(errors.items()):
            print(f"  {s}: {e}")

    daily = defaultdict(lambda: {"realized": 0.0, "trades": 0})
    max_daily_loss = CAPITAL * MAX_DAILY_LOSS_PCT / 100.0
    risk_budget = CAPITAL * RPT_PCT / 100.0
    rows = []

    for _, t in df.iterrows():
        date = str(t["date"])
        state = daily[date]
        entry = float(t["entry"])
        side = str(t["direction"]).upper()
        symbol = str(t["symbol"])
        per_share_risk = entry * SL_PCT
        qty_risk = int(risk_budget / per_share_risk) if per_share_risk > 0 else 0
        mps = margin_map.get(symbol)
        qty_margin = int((AVAILABLE_MARGIN * MAX_POSITION_PCT / 100.0) / mps) if mps else 0
        qty = min(qty_risk, qty_margin)
        realized_loss_used = max(0.0, -state["realized"])
        proposed_risk = per_share_risk * qty
        remaining_daily_risk = max(0.0, max_daily_loss - realized_loss_used)

        reason = "ACCEPTED"
        if state["trades"] >= MAX_TRADES_PER_DAY:
            reason = "MAX_TRADES_PER_DAY"
        elif qty <= 0:
            reason = "ZERO_QTY_OR_MARGIN"
        elif realized_loss_used + proposed_risk > max_daily_loss + 1e-9:
            reason = "DAILY_RISK_BLOCK"

        rebuilt = None
        # Rebuild outcome at the proposed qty even for blocked trades so we can
        # quantify what was actually being filtered out. This is counterfactual.
        if qty > 0:
            day = load_day(symbol, pd.Timestamp(t["signal_ts"]))
            rebuilt = simulate(day, side, entry, qty)

        row = dict(t)
        row.update({
            "decision_075_25": reason,
            "margin_per_share_now": mps,
            "qty_risk_075": qty_risk,
            "qty_margin_25": qty_margin,
            "qty_proposed": qty,
            "per_share_risk_040": per_share_risk,
            "proposed_risk_rupees": proposed_risk,
            "realized_loss_used_before": realized_loss_used,
            "remaining_daily_risk_before": remaining_daily_risk,
        })
        if rebuilt:
            row.update(rebuilt)
        rows.append(row)

        # Only accepted trades alter sequential daily state.
        if reason == "ACCEPTED" and rebuilt is not None:
            state["realized"] += float(rebuilt["sim_net_rebuilt"])
            state["trades"] += 1

    out = pd.DataFrame(rows)
    out.to_csv(OUT / "all_22_accepted_vs_blocked.csv", index=False)

    acc = out[out["decision_075_25"] == "ACCEPTED"].copy()
    blk = out[out["decision_075_25"] != "ACCEPTED"].copy()

    def summarize(name, x):
        sim = pd.to_numeric(x["sim_net_rebuilt"], errors="coerce")
        hist = pd.to_numeric(x.get("historical_net"), errors="coerce")
        print(f"\n===== {name} =====")
        print("count:", len(x))
        print("rebuilt winners:", int((sim > 0).sum()))
        print("rebuilt losers/flat:", int((sim <= 0).sum()))
        print("rebuilt net:", round(float(sim.sum()), 4))
        print("historical winners:", int(x.get("historical_winner", pd.Series(False, index=x.index)).map(truthy).sum()))
        print("historical net:", round(float(hist.sum()), 4) if hist is not None else "NA")
        print("SL hits:", int(x.get("sim_exit_rebuilt", pd.Series('', index=x.index)).astype(str).str.startswith("SL_").sum()))
        for col in ["adx14","ema_distance_atr","volume_ratio20","atr_multiple","clv","confirmation_count","expected_gross_proxy","proposed_risk_rupees","qty_proposed","margin_per_share_now"]:
            if col in x.columns:
                s = pd.to_numeric(x[col], errors="coerce").dropna()
                if len(s):
                    print(f"{col}: median={s.median():.4f} mean={s.mean():.4f} p25={s.quantile(.25):.4f} p75={s.quantile(.75):.4f}")

    summarize("ACCEPTED 9", acc)
    summarize("BLOCKED 13", blk)

    print("\n===== BLOCK REASONS =====")
    print(out["decision_075_25"].value_counts().to_string())

    print("\n===== PATH MIX =====")
    for name, x in [("ACCEPTED", acc), ("BLOCKED", blk)]:
        breakout = x["breakout"].map(truthy).sum() if "breakout" in x else 0
        pullback = x["pullback"].map(truthy).sum() if "pullback" in x else 0
        print(f"{name}: breakout={int(breakout)} pullback={int(pullback)}")

    print("\n===== ACCEPTED TRADE DETAIL =====")
    cols = [c for c in ["date","signal_ts","symbol","direction","entry","breakout","pullback","adx14","ema_distance_atr","volume_ratio20","atr_multiple","clv","qty_proposed","proposed_risk_rupees","sim_exit_rebuilt","sim_net_rebuilt","historical_net"] if c in acc.columns]
    print(acc[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== BLOCKED TRADE DETAIL =====")
    cols2 = [c for c in ["date","signal_ts","symbol","direction","entry","breakout","pullback","adx14","ema_distance_atr","volume_ratio20","atr_multiple","clv","qty_proposed","proposed_risk_rupees","remaining_daily_risk_before","decision_075_25","sim_exit_rebuilt","sim_net_rebuilt","historical_net"] if c in blk.columns]
    print(blk[cols2].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Compact side-by-side metric table.
    metric_rows = []
    for col in ["adx14","ema_distance_atr","volume_ratio20","atr_multiple","clv","confirmation_count","expected_gross_proxy","proposed_risk_rupees","qty_proposed","margin_per_share_now"]:
        if col not in out.columns:
            continue
        a = pd.to_numeric(acc[col], errors="coerce").dropna()
        b = pd.to_numeric(blk[col], errors="coerce").dropna()
        metric_rows.append({
            "metric": col,
            "accepted_median": a.median() if len(a) else None,
            "blocked_median": b.median() if len(b) else None,
            "accepted_mean": a.mean() if len(a) else None,
            "blocked_mean": b.mean() if len(b) else None,
        })
    pd.DataFrame(metric_rows).to_csv(OUT / "accepted_vs_blocked_metrics.csv", index=False)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
