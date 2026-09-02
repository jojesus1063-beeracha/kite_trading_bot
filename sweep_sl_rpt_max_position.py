#!/usr/bin/env python3
"""Research-only sweep for Breakout+Pullback replay.

Sweeps:
  SL: 0.400, 0.425, 0.450, 0.475, 0.500 percent
  RPT: 0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00, 1.50, 2.00 percent
  MAX_POSITION_SIZE_PCT: 25, 40, 50, 60, 75, 100 percent

Uses the live RiskManager sizing formula exactly:
    qty_risk = floor((CAPITAL * RPT/100) / abs(entry-stop))

Uses the live executor's margin-cap formula with CURRENT Kite MIS margin/share:
    budget = available_margin * MAX_POSITION_SIZE_PCT/100
    qty_margin = floor(budget / current_required_margin_per_share)
    qty = min(qty_risk, qty_margin)

Important: historical broker margin-per-share is not stored, so current Kite MIS
margin/share is a proxy for the historical cap. The script reports this explicitly.
It enforces the current 0.5% daily-loss/risk budget sequentially and max 5 trades/day.
Research-only: never places orders or changes live state.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from auth import get_kite_client
from costs import net_pnl_for_trade

TRADE_FILE = Path("runtime/proposed_logic_broker_sl_replay/trade_level.csv")
CANDLE_DIR = Path("runtime/trade_replay_history/candles_3minute")
OUT = Path("runtime/sl_rpt_max_position_sweep")
OUT.mkdir(parents=True, exist_ok=True)

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0
MAX_DAILY_LOSS_PCT = 0.50
MAX_TRADES_PER_DAY = 5

SL_LEVELS = [0.00400, 0.00425, 0.00450, 0.00475, 0.00500]
RPT_LEVELS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00, 1.50, 2.00]
MAX_POSITION_LEVELS = [25.0, 40.0, 50.0, 60.0, 75.0, 100.0]
T1_PCT = 0.005
T2_PCT = 0.010


def truthy(v):
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def load_selected():
    df = pd.read_csv(TRADE_FILE)
    df = df[df["breakout_pullback"].map(truthy)].copy()
    df["signal_ts"] = pd.to_datetime(df["signal_ts"], errors="coerce")
    df = df.dropna(subset=["signal_ts", "entry", "direction", "symbol"])
    df = df.sort_values(["date", "signal_ts", "trade_index"]).reset_index(drop=True)
    return df


def current_margin_per_share(kite, symbols):
    mapping = {}
    errors = {}
    for symbol in sorted(set(symbols)):
        # Entry direction can change order margin slightly in unusual cases; equity MIS
        # is normally symmetric. Query BUY quantity=1 as a stable cap proxy.
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
            result = kite.order_margins(params)
            total = float(result[0].get("total"))
            if total <= 0:
                raise ValueError(f"non-positive margin {total}")
            mapping[symbol] = total
        except Exception as exc:
            errors[symbol] = str(exc)
    return mapping, errors


def load_day_candles(symbol, ts):
    p = CANDLE_DIR / f"{symbol.replace('/', '_')}.parquet"
    if not p.exists():
        return None
    c = pd.read_parquet(p)
    c["timestamp"] = pd.to_datetime(c["timestamp"], errors="coerce")
    c = c.dropna(subset=["timestamp"])
    candle_tz = c["timestamp"].dt.tz
    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert(candle_tz)
    d = ts.date()
    day = c[(c["timestamp"].dt.date == d) & (c["timestamp"] >= ts)].copy()
    return day.sort_values("timestamp")


def simulate_path(day, side, entry, qty, sl_pct):
    if qty <= 0 or day is None or day.empty:
        return None
    buy = side == "BUY"
    stop = entry * (1 - sl_pct if buy else 1 + sl_pct)
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
            # conservative same-candle ordering
            if stop_hit:
                exits.append((qty, stop))
                exit_name = f"SL_{sl_pct*100:.3f}"
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
    # Exact costs for split exits: sum per partial round-trip using proportional entry leg.
    costs = 0.0
    for q, px in exits:
        if q <= 0:
            continue
        r = net_pnl_for_trade(side, int(q), float(entry), float(px))
        gross += float(r["gross_pnl"])
        costs += float(r["costs"])
    return {
        "gross": gross,
        "costs": costs,
        "net": gross-costs,
        "exit": exit_name,
        "stop": stop,
        "t1": t1,
        "t2": t2,
    }


def max_drawdown(pnls):
    eq = 0.0
    peak = 0.0
    worst = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        worst = min(worst, eq-peak)
    return worst


def run_combo(df, margin_map, sl_pct, rpt_pct, max_pos_pct):
    daily = defaultdict(lambda: {"realized": 0.0, "trades": 0})
    rows = []
    blocked_daily = 0
    zero_qty = 0
    margin_capped = 0
    margin_missing = 0

    max_daily_loss = CAPITAL * MAX_DAILY_LOSS_PCT / 100.0
    risk_budget = CAPITAL * rpt_pct / 100.0

    for _, t in df.iterrows():
        date = str(t["date"])
        state = daily[date]
        if state["trades"] >= MAX_TRADES_PER_DAY:
            blocked_daily += 1
            continue

        entry = float(t["entry"])
        side = str(t["direction"]).upper()
        symbol = str(t["symbol"])
        per_share_risk = entry * sl_pct
        if per_share_risk <= 0:
            zero_qty += 1
            continue
        qty_risk = int(risk_budget / per_share_risk)
        if qty_risk <= 0:
            zero_qty += 1
            continue

        mps = margin_map.get(symbol)
        if mps is None or not math.isfinite(mps) or mps <= 0:
            margin_missing += 1
            continue
        margin_budget = AVAILABLE_MARGIN * max_pos_pct / 100.0
        qty_margin = int(margin_budget / mps)
        qty = min(qty_risk, qty_margin)
        if qty < qty_risk:
            margin_capped += 1
        if qty <= 0:
            zero_qty += 1
            continue

        proposed_risk = per_share_risk * qty
        realized_loss_used = max(0.0, -state["realized"])
        # Max open positions is 1 in live. Sequential replay means no concurrent open risk.
        if realized_loss_used + proposed_risk > max_daily_loss + 1e-9:
            blocked_daily += 1
            continue

        day = load_day_candles(symbol, pd.Timestamp(t["signal_ts"]))
        sim = simulate_path(day, side, entry, qty, sl_pct)
        if sim is None:
            continue
        state["realized"] += sim["net"]
        state["trades"] += 1
        rows.append({
            "date": date, "symbol": symbol, "side": side, "entry": entry,
            "qty_risk": qty_risk, "qty_margin": qty_margin, "qty": qty,
            "margin_per_share": mps, "proposed_risk": proposed_risk,
            **sim,
        })

    r = pd.DataFrame(rows)
    if r.empty:
        return {
            "sl_pct": sl_pct*100, "rpt_pct": rpt_pct, "max_position_pct": max_pos_pct,
            "trades": 0, "wins": 0, "losses_flat": 0, "win_rate_pct": 0.0,
            "gross": 0.0, "costs": 0.0, "net": 0.0, "profit_factor": None,
            "avg_net": 0.0, "max_drawdown": 0.0, "avg_qty": 0.0,
            "margin_capped_trades": margin_capped, "daily_risk_blocked": blocked_daily,
            "zero_qty": zero_qty, "margin_missing": margin_missing, "sl_hits": 0,
            "return_on_planned_risk": None,
        }

    wins = int((r["net"] > 0).sum())
    losses = int((r["net"] <= 0).sum())
    pos = float(r.loc[r["net"] > 0, "net"].sum())
    neg = abs(float(r.loc[r["net"] < 0, "net"].sum()))
    planned_risk_total = float(r["proposed_risk"].sum())
    return {
        "sl_pct": sl_pct*100, "rpt_pct": rpt_pct, "max_position_pct": max_pos_pct,
        "trades": len(r), "wins": wins, "losses_flat": losses,
        "win_rate_pct": wins/len(r)*100.0,
        "gross": float(r["gross"].sum()), "costs": float(r["costs"].sum()),
        "net": float(r["net"].sum()), "profit_factor": (pos/neg if neg > 0 else None),
        "avg_net": float(r["net"].mean()), "max_drawdown": max_drawdown(r["net"].tolist()),
        "avg_qty": float(r["qty"].mean()), "margin_capped_trades": margin_capped,
        "daily_risk_blocked": blocked_daily, "zero_qty": zero_qty,
        "margin_missing": margin_missing,
        "sl_hits": int(r["exit"].astype(str).str.startswith("SL_").sum()),
        "return_on_planned_risk": (float(r["net"].sum())/planned_risk_total if planned_risk_total > 0 else None),
    }


def main():
    df = load_selected()
    print(f"Breakout+Pullback candidates: {len(df)}")
    kite = get_kite_client()
    margin_map, margin_errors = current_margin_per_share(kite, df["symbol"].tolist())
    print(f"Current MIS margin/share resolved: {len(margin_map)}/{df['symbol'].nunique()}")
    if margin_errors:
        print("Margin lookup errors:")
        for s, e in sorted(margin_errors.items()):
            print(f"  {s}: {e}")

    pd.DataFrame([{"symbol": k, "margin_per_share": v} for k, v in sorted(margin_map.items())]).to_csv(OUT/"current_margin_per_share.csv", index=False)

    out = []
    for sl in SL_LEVELS:
        for rpt in RPT_LEVELS:
            for mp in MAX_POSITION_LEVELS:
                out.append(run_combo(df, margin_map, sl, rpt, mp))
    res = pd.DataFrame(out)
    res.to_csv(OUT/"all_270_combinations.csv", index=False)

    # Ranking: require at least 5 executed trades so tiny samples don't dominate.
    eligible = res[res["trades"] >= 5].copy()
    ranked_net = eligible.sort_values(["net", "profit_factor"], ascending=[False, False])
    ranked_eff = eligible.sort_values(["return_on_planned_risk", "net"], ascending=[False, False])
    ranked_net.head(30).to_csv(OUT/"top30_by_net.csv", index=False)
    ranked_eff.head(30).to_csv(OUT/"top30_by_risk_efficiency.csv", index=False)

    print("\n===== TOP 20 BY NET P&L =====")
    cols = ["sl_pct","rpt_pct","max_position_pct","trades","wins","losses_flat","win_rate_pct","sl_hits","avg_qty","margin_capped_trades","daily_risk_blocked","gross","costs","net","profit_factor","max_drawdown","return_on_planned_risk"]
    print(ranked_net[cols].head(20).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== TOP 20 BY RISK EFFICIENCY =====")
    print(ranked_eff[cols].head(20).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== CURRENT-LIKE ROW (SL=.5 RPT=.2 MAXPOS=50) =====")
    cur = res[(res.sl_pct.round(3)==0.500) & (res.rpt_pct==0.20) & (res.max_position_pct==50.0)]
    print(cur[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== RPT > DAILY LOSS LIMIT EFFECT =====")
    high = res[res.rpt_pct > MAX_DAILY_LOSS_PCT].groupby("rpt_pct").agg(trades=("trades","mean"), daily_risk_blocked=("daily_risk_blocked","mean"), net=("net","mean")).reset_index()
    print(high.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    summary = {
        "candidate_trades": int(len(df)),
        "combinations": int(len(res)),
        "capital": CAPITAL,
        "available_margin_proxy": AVAILABLE_MARGIN,
        "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "margin_symbols_resolved": len(margin_map),
        "margin_symbols_failed": margin_errors,
        "important_limitation": "MAX_POSITION_SIZE_PCT uses current Kite MIS margin/share as a proxy because historical broker margin/share was not stored.",
        "best_by_net": ranked_net.iloc[0].to_dict() if not ranked_net.empty else None,
        "best_by_risk_efficiency": ranked_eff.iloc[0].to_dict() if not ranked_eff.empty else None,
    }
    (OUT/"summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {OUT}")

if __name__ == "__main__":
    main()
