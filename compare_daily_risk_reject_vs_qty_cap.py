#!/usr/bin/env python3
"""Research-only A/B replay: current all-or-nothing daily-risk rejection vs qty capping.

Keeps Breakout+Pullback entries and existing replay exit model unchanged.
Does NOT place orders or modify live state.

Mode A (CURRENT_REJECT):
    qty = min(qty_risk, qty_margin)
    reject whole trade if realized_loss_used + proposed_risk > daily budget.

Mode B (QTY_CAP):
    qty_daily = floor(remaining_daily_risk / per_share_risk)
    qty = min(qty_risk, qty_margin, qty_daily)
    trade whenever qty >= 1.

Grid:
    SL = 0.40%
    RPT = 0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.00%
    MAX_POSITION = 25..50% in 1% increments
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

import sweep_sl_rpt_max_position as base

OUT = Path("runtime/daily_risk_reject_vs_qty_cap")
OUT.mkdir(parents=True, exist_ok=True)

SL_LEVELS = [0.00400]
RPT_LEVELS = [0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.00]
MAX_POSITION_LEVELS = [float(x) for x in range(25, 51)]


def run_combo(df, margin_map, sl_pct, rpt_pct, max_pos_pct, mode):
    daily = defaultdict(lambda: {"realized": 0.0, "trades": 0})
    rows = []
    blocked_daily = 0
    zero_qty = 0
    margin_capped = 0
    daily_qty_capped = 0
    margin_missing = 0

    max_daily_loss = base.CAPITAL * base.MAX_DAILY_LOSS_PCT / 100.0
    risk_budget = base.CAPITAL * rpt_pct / 100.0

    for _, t in df.iterrows():
        date = str(t["date"])
        state = daily[date]
        if state["trades"] >= base.MAX_TRADES_PER_DAY:
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

        margin_budget = base.AVAILABLE_MARGIN * max_pos_pct / 100.0
        qty_margin = int(margin_budget / mps)
        qty_pre_daily = min(qty_risk, qty_margin)
        if qty_pre_daily < qty_risk:
            margin_capped += 1
        if qty_pre_daily <= 0:
            zero_qty += 1
            continue

        realized_loss_used = max(0.0, -state["realized"])
        remaining_daily_risk = max(0.0, max_daily_loss - realized_loss_used)

        if mode == "CURRENT_REJECT":
            qty_daily = int(remaining_daily_risk / per_share_risk)
            qty = qty_pre_daily
            proposed_risk = per_share_risk * qty
            if realized_loss_used + proposed_risk > max_daily_loss + 1e-9:
                blocked_daily += 1
                continue
        elif mode == "QTY_CAP":
            qty_daily = int((remaining_daily_risk + 1e-12) / per_share_risk)
            qty = min(qty_pre_daily, qty_daily)
            if qty < qty_pre_daily:
                daily_qty_capped += 1
            if qty <= 0:
                blocked_daily += 1
                continue
            proposed_risk = per_share_risk * qty
        else:
            raise ValueError(mode)

        day = base.load_day_candles(symbol, pd.Timestamp(t["signal_ts"]))
        sim = base.simulate_path(day, side, entry, qty, sl_pct)
        if sim is None:
            continue

        state["realized"] += sim["net"]
        state["trades"] += 1
        rows.append({
            "mode": mode,
            "date": date,
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "qty_risk": qty_risk,
            "qty_margin": qty_margin,
            "qty_daily": qty_daily,
            "qty_pre_daily": qty_pre_daily,
            "qty": qty,
            "margin_per_share": mps,
            "remaining_daily_risk_before": remaining_daily_risk,
            "proposed_risk": proposed_risk,
            **sim,
        })

    r = pd.DataFrame(rows)
    common = {
        "mode": mode,
        "sl_pct": sl_pct * 100,
        "rpt_pct": rpt_pct,
        "max_position_pct": max_pos_pct,
        "margin_capped_trades": margin_capped,
        "daily_qty_capped_trades": daily_qty_capped,
        "daily_risk_blocked": blocked_daily,
        "zero_qty": zero_qty,
        "margin_missing": margin_missing,
    }
    if r.empty:
        return {**common,
            "trades": 0, "wins": 0, "losses_flat": 0, "win_rate_pct": 0.0,
            "gross": 0.0, "costs": 0.0, "net": 0.0, "profit_factor": None,
            "avg_net": 0.0, "max_drawdown": 0.0, "avg_qty": 0.0,
            "sl_hits": 0, "planned_risk_total": 0.0, "return_on_planned_risk": None,
        }, r

    wins = int((r["net"] > 0).sum())
    losses = int((r["net"] <= 0).sum())
    pos = float(r.loc[r["net"] > 0, "net"].sum())
    neg = abs(float(r.loc[r["net"] < 0, "net"].sum()))
    planned_risk_total = float(r["proposed_risk"].sum())
    summary = {**common,
        "trades": len(r), "wins": wins, "losses_flat": losses,
        "win_rate_pct": wins / len(r) * 100.0,
        "gross": float(r["gross"].sum()), "costs": float(r["costs"].sum()),
        "net": float(r["net"].sum()), "profit_factor": (pos / neg if neg > 0 else None),
        "avg_net": float(r["net"].mean()), "max_drawdown": base.max_drawdown(r["net"].tolist()),
        "avg_qty": float(r["qty"].mean()),
        "sl_hits": int(r["exit"].astype(str).str.startswith("SL_").sum()),
        "planned_risk_total": planned_risk_total,
        "return_on_planned_risk": (float(r["net"].sum()) / planned_risk_total if planned_risk_total > 0 else None),
    }
    return summary, r


def main():
    df = base.load_selected()
    print(f"Breakout+Pullback candidates: {len(df)}")
    kite = base.get_kite_client()
    margin_map, margin_errors = base.current_margin_per_share(kite, df["symbol"].tolist())
    print(f"Current MIS margin/share resolved: {len(margin_map)}/{df['symbol'].nunique()}")
    if margin_errors:
        print("Margin lookup errors:")
        for s, e in sorted(margin_errors.items()):
            print(f"  {s}: {e}")

    pd.DataFrame([{"symbol": k, "margin_per_share": v} for k, v in sorted(margin_map.items())]).to_csv(
        OUT / "current_margin_per_share.csv", index=False
    )

    summaries = []
    detail_frames = []
    for mode in ["CURRENT_REJECT", "QTY_CAP"]:
        for sl in SL_LEVELS:
            for rpt in RPT_LEVELS:
                for mp in MAX_POSITION_LEVELS:
                    summary, detail = run_combo(df, margin_map, sl, rpt, mp, mode)
                    summaries.append(summary)
                    if not detail.empty:
                        detail = detail.copy()
                        detail["sl_pct"] = sl * 100
                        detail["rpt_pct"] = rpt
                        detail["max_position_pct"] = mp
                        detail_frames.append(detail)

    res = pd.DataFrame(summaries)
    res.to_csv(OUT / "all_combinations.csv", index=False)
    if detail_frames:
        pd.concat(detail_frames, ignore_index=True).to_csv(OUT / "trade_level.csv", index=False)

    eligible = res[res["trades"] >= 5].copy()
    top = eligible.sort_values(["net", "profit_factor"], ascending=[False, False])
    top.to_csv(OUT / "top_by_net.csv", index=False)

    cols = [
        "mode", "rpt_pct", "max_position_pct", "trades", "wins", "losses_flat",
        "win_rate_pct", "sl_hits", "avg_qty", "margin_capped_trades",
        "daily_qty_capped_trades", "daily_risk_blocked", "gross", "costs", "net",
        "profit_factor", "max_drawdown", "return_on_planned_risk"
    ]

    print("\n===== TOP 30 BY NET P&L =====")
    print(top[cols].head(30).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== BEST CURRENT_REJECT BY RPT =====")
    a = res[res["mode"] == "CURRENT_REJECT"].sort_values(["rpt_pct", "net"], ascending=[True, False]).groupby("rpt_pct").head(1)
    print(a[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== BEST QTY_CAP BY RPT =====")
    b = res[res["mode"] == "QTY_CAP"].sort_values(["rpt_pct", "net"], ascending=[True, False]).groupby("rpt_pct").head(1)
    print(b[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== DIRECT A/B AT MAX_POSITION 25% =====")
    ab25 = res[res.max_position_pct == 25.0].sort_values(["rpt_pct", "mode"])
    print(ab25[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n===== DIRECT A/B AT MAX_POSITION 50% =====")
    ab50 = res[res.max_position_pct == 50.0].sort_values(["rpt_pct", "mode"])
    print(ab50[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
