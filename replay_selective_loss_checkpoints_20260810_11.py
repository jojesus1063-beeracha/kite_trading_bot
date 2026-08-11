#!/usr/bin/env python3
"""Selective post-entry loss-filter research for the current 3-minute PAPER policy.

ANALYSIS ONLY. This script never imports/runs main.py and cannot place orders.

Why this study exists
---------------------
The Aug-11 winner/loser minute-path research found that severe adverse excursion
was much more selective than the broad +10m failed-development rule. In
particular, at the +8m checkpoint, MAE <= -0.15% flagged losers without flagging
winners in that one-day sample. The previous A replay also showed that changing
exit time can free a symbol and create additional entries, which mixes exit
quality with re-entry effects.

This study therefore has TWO phases for each candidate:

1. FIXED_BASELINE_ENTRIES
   Replay the exact entries, directions and quantities accepted by the current
   3-minute baseline. Only the exit changes. Earlier exits DO NOT create extra
   entries. This isolates whether the exit itself helps or harms the same trades.

2. REALISTIC_REENTRY
   Replay the source opportunities chronologically with the candidate active.
   Earlier exits may free a symbol and allow later opportunities, matching the
   production interaction more closely.

Candidate rules are intentionally small and pre-specified rather than an
optimization grid. Every candidate is a ONE-SHOT checkpoint: once the first
historical minute observation reaches the checkpoint age, the rule is evaluated
exactly once. If it does not fire then, it never fires later in that trade.

Candidates
----------
CP8_MAE15_NEG
    At first observation age >= 8m:
      MAE <= -0.15% AND current P/L < 0

CP9_MAE15_NEG
    Same rule at >=9m, aligned approximately with three completed 3m intervals.

CP9_MAE15_NEG_MFE15
    At >=9m:
      MAE <= -0.15% AND current P/L < 0 AND MFE < +0.15%

CP9_MAE20_NEG
    At >=9m:
      MAE <= -0.20% AND current P/L < 0

Held constant
-------------
- current 3-minute entry timing/direction
- stored stock 15m ADX; <20 BLOCK, 20-<40 REVERSE, >=40 NORMAL
- RSI extreme override after ADX gate
- 0.20% risk/trade and 0.45% sizing geometry
- sticky 5% realized-loss halt and aggregate-risk admission guard
- 0.75% emergency stop
- hybrid / native MAE / MFE / dead-trade / square-off exits
- existing cost model

Data integrity
--------------
Uses the fail-closed historical loader from
replay_failed_development_variants_20260810_11.py. Partial Kite history aborts
rather than being treated as a strategy rejection.

This is still a same-source-opportunity counterfactual. It does not recreate
signals the historical bot never generated.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import replay_failed_development_variants_20260810_11 as fd

base = fd.base
current = fd.current
IST = fd.IST
DATES = ("2026-08-11", "2026-08-10")
BASELINE = "BASELINE"
OUT_SUMMARY = Path("/tmp/selective_loss_checkpoint_summary.csv")
OUT_TRADES = Path("/tmp/selective_loss_checkpoint_trades.csv")


@dataclass(frozen=True)
class Candidate:
    name: str
    checkpoint_minutes: float
    mae_threshold_pct: float
    require_current_negative: bool = True
    max_mfe_pct: float | None = None


CANDIDATES = (
    Candidate("CP8_MAE15_NEG", 8.0, -0.15, True, None),
    Candidate("CP9_MAE15_NEG", 9.0, -0.15, True, None),
    Candidate("CP9_MAE15_NEG_MFE15", 9.0, -0.15, True, 0.15),
    Candidate("CP9_MAE20_NEG", 9.0, -0.20, True, None),
)
CANDIDATE_BY_NAME = {c.name: c for c in CANDIDATES}


def checkpoint_decision(
    candidate: Candidate,
    entry_age_minutes: float,
    mfe_from_entry_pct: float,
    mae_from_entry_pct: float,
    current_pct: float,
    already_evaluated: bool,
):
    """Return (evaluated_now_or_before, exit_reason_or_None).

    The candidate is evaluated only once: on the first observation at or after
    its checkpoint age. Calling this helper after that with already_evaluated=True
    can never trigger a late exit.
    """
    if already_evaluated:
        return True, None
    if entry_age_minutes < candidate.checkpoint_minutes:
        return False, None

    passed = mae_from_entry_pct <= candidate.mae_threshold_pct
    if candidate.require_current_negative:
        passed = passed and current_pct < 0.0
    if candidate.max_mfe_pct is not None:
        passed = passed and mfe_from_entry_pct < candidate.max_mfe_pct

    reason = f"selective_{candidate.name.lower()}" if passed else None
    return True, reason


def replay_trade_selective(op, direction, qty, df1, df3, candidate, square_off):
    """Mirror current exit stack with one candidate checkpoint inserted.

    Native stop, runner-BE and target checks retain priority. The selective
    checkpoint runs before the slower native MAE/MFE overlays.
    """
    entry = float(op["entry"])
    timer_start = op["signal_start"]
    order_time = op["order_time"]
    strategy_risk = entry * base.STRATEGY_STOP_PCT / 100.0
    sign = 1.0 if direction == "BUY" else -1.0
    emergency_stop = entry - sign * entry * base.EMERGENCY_STOP_PCT / 100.0

    hybrid = base.HYBRID_ENABLED and qty >= 2
    if hybrid:
        scalp_qty = int(math.floor(qty * base.SCALP_FRACTION))
        scalp_qty = min(max(scalp_qty, 1), qty - 1)
        scalp_target = entry + sign * strategy_risk * base.SCALP_R
        runner_target = entry + sign * strategy_risk * base.RUNNER_R
    else:
        scalp_qty = 0
        scalp_target = None
        runner_target = entry + sign * entry * base.NON_HYBRID_TARGET_PCT / 100.0

    rows = df1.loc[
        (df1["date"] >= order_time.floor("min")) & (df1["date"] <= square_off)
    ].copy()
    if rows.empty:
        return None

    remaining = int(qty)
    scalp_pending = hybrid
    be_active = False
    legs = []
    max_mfe = 0.0
    min_mae = 0.0
    checkpoint_evaluated = False
    trigger = None
    trigger_age = None
    trigger_mfe = None
    trigger_mae = None
    trigger_current = None

    def close_leg(now, price, amount, reason):
        nonlocal remaining
        amount = min(int(amount), remaining)
        if amount <= 0:
            return
        costs = base.cost_leg(direction, amount, entry, price)
        legs.append(
            {
                "time": now,
                "qty": amount,
                "price": float(price),
                "reason": reason,
                **costs,
            }
        )
        remaining -= amount

    for _, row in rows.iterrows():
        now = row["date"]
        price = float(row["close"])

        since_strategy = df1.loc[
            (df1["date"] >= timer_start.floor("min")) & (df1["date"] <= now)
        ]
        mfe_strategy, mae_strategy, current_strategy, giveback = base.excursions(
            direction, entry, since_strategy, price
        )
        max_mfe = max(max_mfe, mfe_strategy)
        min_mae = min(min_mae, mae_strategy)
        strategy_age = max(0.0, (now - timer_start).total_seconds() / 60.0)

        since_entry = rows.loc[rows["date"] <= now]
        mfe_entry, mae_entry, current_entry, _ = base.excursions(
            direction, entry, since_entry, price
        )
        entry_age = max(0.0, (now - order_time).total_seconds() / 60.0)

        # 1) Native executable emergency stop.
        hit_emergency = (
            price <= emergency_stop if direction == "BUY" else price >= emergency_stop
        )
        if not be_active and hit_emergency:
            close_leg(now, price, remaining, "paper_emergency_stop_0_75")
            break

        # 2) Existing runner breakeven after scalp.
        if be_active:
            hit_be = price <= entry if direction == "BUY" else price >= entry
            if hit_be:
                close_leg(now, price, remaining, "hybrid_breakeven_stop")
                break

        # 3) Existing target/hybrid handling.
        if scalp_pending:
            hit = price >= scalp_target if direction == "BUY" else price <= scalp_target
            if hit:
                close_leg(now, price, scalp_qty, "hybrid_scalp_1r")
                scalp_pending = False
                be_active = base.MOVE_BE
                continue
        else:
            hit = price >= runner_target if direction == "BUY" else price <= runner_target
            if hit:
                close_leg(
                    now,
                    price,
                    remaining,
                    "hybrid_runner_2r" if hybrid else "fixed_target",
                )
                break

        # 4) One-shot selective checkpoint.
        checkpoint_evaluated, reason = checkpoint_decision(
            candidate,
            entry_age,
            mfe_entry,
            mae_entry,
            current_entry,
            checkpoint_evaluated,
        )
        if reason:
            trigger = reason
            trigger_age = entry_age
            trigger_mfe = mfe_entry
            trigger_mae = mae_entry
            trigger_current = current_entry
            close_leg(now, price, remaining, reason)
            break

        # 5) Existing MAE/adverse-trend overlay.
        if strategy_age > base.MAE_MIN_AGE:
            adverse = base.adverse_three(direction, df3, now)
            if (
                mae_strategy <= base.MAE_THRESHOLD
                and current_strategy <= base.CURRENT_LOSS_THRESHOLD
                and mfe_strategy < base.MAX_MFE_FAILURE
                and adverse
            ):
                close_leg(now, price, remaining, "mae_adverse_trend_10m")
                break

        # 6) Existing MFE/time overlay.
        reason = base.mfe_reason(strategy_age, mfe_strategy, current_strategy, giveback)
        if reason:
            close_leg(now, price, remaining, reason)
            break

        if now >= square_off:
            close_leg(now, price, remaining, "square_off")
            break

    if remaining > 0:
        last = rows.iloc[-1]
        close_leg(last["date"], float(last["close"]), remaining, "square_off_fallback")

    return {
        "legs": legs,
        "exit_time": max(x["time"] for x in legs),
        "net": sum(float(x["net"]) for x in legs),
        "gross": sum(float(x["gross"]) for x in legs),
        "costs": sum(float(x["costs"]) for x in legs),
        "mfe": max_mfe,
        "mae": min_mae,
        "reasons": " + ".join(x["reason"] for x in legs),
        "selective_trigger": trigger,
        "trigger_age": trigger_age,
        "trigger_mfe": trigger_mfe,
        "trigger_mae": trigger_mae,
        "trigger_current": trigger_current,
    }


def summarize_trades(date, mode, candidate_name, trades, rejected=None):
    gross = sum(float(t["replay"]["gross"]) for t in trades)
    costs = sum(float(t["replay"]["costs"]) for t in trades)
    net = sum(float(t["replay"]["net"]) for t in trades)
    wins = sum(1 for t in trades if t["replay"]["net"] > 0)
    losses = sum(1 for t in trades if t["replay"]["net"] < 0)
    flats = len(trades) - wins - losses

    legs = sorted(
        [leg for t in trades for leg in t["replay"]["legs"]],
        key=lambda x: x["time"],
    )
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for leg in legs:
        running += float(leg["net"])
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    return {
        "date": date,
        "mode": mode,
        "candidate": candidate_name,
        "trades": trades,
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": wins / len(trades) * 100.0 if trades else 0.0,
        "gross": gross,
        "costs": costs,
        "net": net,
        "return_pct": net / base.CAPITAL * 100.0,
        "max_dd": max_dd,
        "trigger_count": sum(bool(t["replay"].get("selective_trigger")) for t in trades),
        "rejections": Counter(reason for _, reason in (rejected or [])),
    }


def fixed_entry_candidate(date, baseline, minute, three, candidate, square_off):
    """Change only exits for the exact baseline accepted trades."""
    changed = []
    for original in baseline["accepted"]:
        op = dict(original)
        result = replay_trade_selective(
            op,
            original["direction"],
            original["qty"],
            minute[original["symbol"]],
            three[original["symbol"]],
            candidate,
            square_off,
        )
        if result is None:
            raise RuntimeError(
                f"DATA INTEGRITY FAILURE fixed replay {date} {original['symbol']}"
            )
        changed.append(
            {
                **original,
                "replay": result,
                "baseline_replay": original["replay"],
            }
        )

    result = summarize_trades(date, "FIXED_BASELINE_ENTRIES", candidate.name, changed)
    result["winner_harmed"] = sum(
        1
        for t in changed
        if t["baseline_replay"]["net"] > 0
        and t["replay"]["net"] < t["baseline_replay"]["net"] - 1e-9
    )
    result["winner_flipped"] = sum(
        1
        for t in changed
        if t["baseline_replay"]["net"] > 0 and t["replay"]["net"] <= 0
    )
    result["loser_improved"] = sum(
        1
        for t in changed
        if t["baseline_replay"]["net"] < 0
        and t["replay"]["net"] > t["baseline_replay"]["net"] + 1e-9
    )
    result["loser_worsened"] = sum(
        1
        for t in changed
        if t["baseline_replay"]["net"] < 0
        and t["replay"]["net"] < t["baseline_replay"]["net"] - 1e-9
    )
    return result


def realistic_candidate(date, opportunities, minute, three, candidate, square_off):
    """Chronological replay where early exits may free a symbol for re-entry."""
    enriched = fd.enrich_current_direction(opportunities, three)
    accepted = []
    rejected = []
    sticky_halt_time = None

    for op in enriched:
        now = op["order_time"]
        prior_legs = fd.completed_legs(accepted, now)
        realized = sum(float(leg["net"]) for leg in prior_legs)

        if sticky_halt_time is None:
            running = 0.0
            for leg in prior_legs:
                running += float(leg["net"])
                if running <= -fd.DAILY_LOSS_LIMIT:
                    sticky_halt_time = leg["time"]
                    break
        if sticky_halt_time is not None and now >= sticky_halt_time:
            rejected.append((op, "DAILY_LOSS_5PCT_STICKY_HALT"))
            continue

        adx = op.get("adx")
        if adx is not None and math.isfinite(float(adx)) and float(adx) < 20.0:
            rejected.append((op, "ADX_LT_20_BLOCK"))
            continue

        direction = op.get("proposed_direction")
        if direction not in {"BUY", "SELL"}:
            rejected.append((op, "DIRECTION_UNAVAILABLE"))
            continue

        per_share_risk = float(op["entry"]) * fd.STRATEGY_STOP_PCT / 100.0
        risk_qty = int(fd.RISK_AMOUNT / per_share_risk) if per_share_risk > 0 else 0
        qty = min(risk_qty, int(op["actual_qty"]))
        if qty <= 0:
            rejected.append((op, "QTY_ZERO_AT_0_2PCT_RISK"))
            continue

        open_now = fd.open_trades_at(accepted, now)
        if any(t["symbol"] == op["symbol"] for t in open_now):
            rejected.append((op, "SYMBOL_ALREADY_OPEN"))
            continue

        realized_loss = max(0.0, -realized)
        open_risk = sum(
            fd.strategy_risk_for(t["entry"], fd.remaining_qty_at(t, now))
            for t in open_now
        )
        proposed_risk = fd.strategy_risk_for(op["entry"], qty)
        aggregate = realized_loss + open_risk + proposed_risk
        if aggregate >= fd.DAILY_LOSS_LIMIT:
            rejected.append((op, "AGGREGATE_DAILY_RISK_GTE_BUDGET"))
            continue

        df1 = minute.get(op["symbol"])
        df3 = three.get(op["symbol"])
        if df1 is None or df1.empty or df3 is None or df3.empty:
            raise RuntimeError(f"DATA INTEGRITY FAILURE realistic {date} {op['symbol']}")

        replay = replay_trade_selective(
            op, direction, qty, df1, df3, candidate, square_off
        )
        if replay is None:
            rejected.append((op, "NO_EXIT_HISTORY"))
            continue

        accepted.append(
            {
                **op,
                "direction": direction,
                "qty": qty,
                "risk_qty": risk_qty,
                "entry_time": now,
                "entry": float(op["entry"]),
                "replay": replay,
            }
        )

    return summarize_trades(date, "REALISTIC_REENTRY", candidate.name, accepted, rejected)


def baseline_as_summary(date, baseline):
    # fd.replay_policy already calculated the current baseline accurately.
    return {
        "date": date,
        "mode": "BASELINE",
        "candidate": BASELINE,
        "trades": baseline["accepted"],
        "trade_count": baseline["trade_count"],
        "wins": baseline["wins"],
        "losses": baseline["losses"],
        "flats": baseline["flats"],
        "win_rate": baseline["win_rate"],
        "gross": baseline["gross"],
        "costs": baseline["costs"],
        "net": baseline["net"],
        "return_pct": baseline["return_pct"],
        "max_dd": baseline["max_drawdown"],
        "trigger_count": 0,
        "rejections": baseline["rejection_counts"],
    }


def print_result(r, baseline_net=None):
    print("\n" + "=" * 124)
    print(f"{r['date']} | {r['mode']} | {r['candidate']}")
    print("=" * 124)
    print(f"TRADES       : {r['trade_count']}")
    print(f"W/L/F        : {r['wins']} / {r['losses']} / {r['flats']} | WR {r['win_rate']:.2f}%")
    print(f"GROSS        : Rs {r['gross']:.2f}")
    print(f"COSTS        : Rs {r['costs']:.2f}")
    print(f"NET          : Rs {r['net']:.2f}")
    print(f"RETURN       : {r['return_pct']:.3f}%")
    print(f"MAX DD       : Rs {r['max_dd']:.2f}")
    print(f"FILTER EXITS : {r['trigger_count']}")
    if baseline_net is not None:
        print(f"VS BASELINE  : Rs {r['net'] - baseline_net:+.2f}")
    if r["mode"] == "FIXED_BASELINE_ENTRIES":
        print(
            "TRADE EFFECT : "
            f"losers improved={r['loser_improved']} | losers worsened={r['loser_worsened']} | "
            f"winners harmed={r['winner_harmed']} | winners flipped={r['winner_flipped']}"
        )
    if r.get("rejections"):
        print("REJECTIONS:")
        for reason, count in r["rejections"].most_common():
            print(f"  {reason:<40} {count}")


def print_trigger_detail(r):
    triggered = [t for t in r["trades"] if t["replay"].get("selective_trigger")]
    if not triggered:
        return
    print("FILTER EXIT DETAIL")
    print(
        f"{'ENTRY':<9} {'SYMBOL':<13} {'DIR':>5} {'QTY':>4} "
        f"{'BASE':>9} {'NEW':>9} {'DELTA':>9} {'AGE':>6} {'MFE%':>7} {'MAE%':>7} {'CUR%':>7}"
    )
    for t in triggered:
        rnew = t["replay"]
        base_net = (
            float(t["baseline_replay"]["net"])
            if t.get("baseline_replay") is not None
            else math.nan
        )
        delta = rnew["net"] - base_net if math.isfinite(base_net) else math.nan
        print(
            f"{t['entry_time'].strftime('%H:%M:%S'):<9} {t['symbol']:<13} "
            f"{t['direction']:>5} {t['qty']:>4} "
            f"{base_net:>9.2f} {rnew['net']:>9.2f} {delta:>+9.2f} "
            f"{float(rnew['trigger_age']):>6.1f} {float(rnew['trigger_mfe']):>7.3f} "
            f"{float(rnew['trigger_mae']):>7.3f} {float(rnew['trigger_current']):>7.3f}"
        )


def row_for_csv(r):
    return {
        "date": r["date"],
        "mode": r["mode"],
        "candidate": r["candidate"],
        "trades": r["trade_count"],
        "wins": r["wins"],
        "losses": r["losses"],
        "flats": r["flats"],
        "win_rate_pct": r["win_rate"],
        "gross": r["gross"],
        "costs": r["costs"],
        "net": r["net"],
        "return_pct": r["return_pct"],
        "max_dd": r["max_dd"],
        "filter_exits": r["trigger_count"],
        "loser_improved": r.get("loser_improved"),
        "loser_worsened": r.get("loser_worsened"),
        "winner_harmed": r.get("winner_harmed"),
        "winner_flipped": r.get("winner_flipped"),
    }


def trade_rows_for_csv(r):
    rows = []
    for t in r["trades"]:
        replay = t["replay"]
        baseline_replay = t.get("baseline_replay")
        rows.append(
            {
                "date": r["date"],
                "mode": r["mode"],
                "candidate": r["candidate"],
                "entry_time": t["entry_time"],
                "symbol": t["symbol"],
                "direction": t["direction"],
                "adx": t.get("adx"),
                "qty": t["qty"],
                "baseline_net": baseline_replay.get("net") if baseline_replay else None,
                "new_net": replay["net"],
                "exit_time": replay["exit_time"],
                "exit_reasons": replay["reasons"],
                "filter_trigger": replay.get("selective_trigger"),
                "trigger_age": replay.get("trigger_age"),
                "trigger_mfe": replay.get("trigger_mfe"),
                "trigger_mae": replay.get("trigger_mae"),
                "trigger_current": replay.get("trigger_current"),
            }
        )
    return rows


def main():
    print("Connecting to Kite for ANALYSIS-ONLY selective-loss checkpoint study...")
    kite = base.get_kite_client()
    all_results = []
    all_trade_rows = []
    by_date = {}

    for date in DATES:
        ctx = fd.configure_date(date)
        opportunities = base.group_history()
        if not opportunities:
            raise SystemExit(f"No source opportunities found for {date}")
        symbols = sorted({op["symbol"] for op in opportunities})
        print(f"\nDAY {date} | source={len(opportunities)} | symbols={len(symbols)}")
        minute, three = fd.fetch_market_data_reliable(kite, symbols, date)

        baseline_raw = fd.replay_policy(
            date, fd.BASELINE, opportunities, minute, three, ctx.square_off
        )
        baseline = baseline_as_summary(date, baseline_raw)

        if date == "2026-08-11" and baseline["trade_count"] < 18:
            raise RuntimeError(
                f"DATA INTEGRITY BLOCK: Aug-11 baseline reconstructed only "
                f"{baseline['trade_count']} trades; expected approximately 20."
            )

        print_result(baseline)
        all_results.append(baseline)
        all_trade_rows.extend(trade_rows_for_csv(baseline))
        by_date[date] = {"baseline": baseline, "fixed": {}, "realistic": {}}

        for candidate in CANDIDATES:
            fixed = fixed_entry_candidate(
                date, baseline_raw, minute, three, candidate, ctx.square_off
            )
            realistic = realistic_candidate(
                date, opportunities, minute, three, candidate, ctx.square_off
            )
            by_date[date]["fixed"][candidate.name] = fixed
            by_date[date]["realistic"][candidate.name] = realistic

            print_result(fixed, baseline["net"])
            print_trigger_detail(fixed)
            print_result(realistic, baseline["net"])

            all_results.extend([fixed, realistic])
            all_trade_rows.extend(trade_rows_for_csv(fixed))
            all_trade_rows.extend(trade_rows_for_csv(realistic))

    print("\n" + "#" * 124)
    print("TWO-DAY SELECTIVE-LOSS DECISION TABLE")
    print("#" * 124)
    b11 = by_date["2026-08-11"]["baseline"]
    b10 = by_date["2026-08-10"]["baseline"]
    baseline_two = b11["net"] + b10["net"]
    print(f"Baseline two-day net: Rs {baseline_two:.2f}")
    print(
        f"{'CANDIDATE':<26} {'FIX11':>9} {'FIX10':>9} {'FIX2D':>9} "
        f"{'R11':>9} {'R10':>9} {'R2D':>9} {'WFLIP':>6} DECISION"
    )

    deployable = []
    for c in CANDIDATES:
        f11 = by_date["2026-08-11"]["fixed"][c.name]
        f10 = by_date["2026-08-10"]["fixed"][c.name]
        r11 = by_date["2026-08-11"]["realistic"][c.name]
        r10 = by_date["2026-08-10"]["realistic"][c.name]
        fixed_two = f11["net"] + f10["net"]
        real_two = r11["net"] + r10["net"]
        winner_flips = f11["winner_flipped"] + f10["winner_flipped"]

        # Deliberately strict research gate: do not call a candidate promising
        # unless it improves the exact baseline trades on BOTH dates, flips no
        # baseline winner to non-positive, and also improves the realistic replay
        # on BOTH dates. This is still not deployment authorization.
        passes = (
            f11["net"] > b11["net"]
            and f10["net"] >= b10["net"]
            and winner_flips == 0
            and r11["net"] > b11["net"]
            and r10["net"] >= b10["net"]
        )
        decision = "PROMISING_RESEARCH" if passes else "REJECT_FOR_NOW"
        if passes:
            deployable.append((real_two, fixed_two, c.name))
        print(
            f"{c.name:<26} {f11['net']:>9.2f} {f10['net']:>9.2f} {fixed_two:>9.2f} "
            f"{r11['net']:>9.2f} {r10['net']:>9.2f} {real_two:>9.2f} "
            f"{winner_flips:>6} {decision}"
        )

    if deployable:
        deployable.sort(reverse=True)
        print(f"\nBEST RESEARCH CANDIDATE: {deployable[0][2]}")
        print("STATUS: promising for additional PAPER validation; NOT deployed by this script.")
    else:
        print("\nBEST RESEARCH CANDIDATE: NONE passes the strict two-day gate.")
        print("STATUS: keep current 3-minute baseline unchanged.")

    pd.DataFrame([row_for_csv(r) for r in all_results]).to_csv(OUT_SUMMARY, index=False)
    pd.DataFrame(all_trade_rows).to_csv(OUT_TRADES, index=False)
    print("\nFILES")
    print(f"  {OUT_SUMMARY}")
    print(f"  {OUT_TRADES}")
    print("DEPLOYMENT: NONE — ANALYSIS ONLY")


if __name__ == "__main__":
    main()
