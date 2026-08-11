#!/usr/bin/env python3
"""Analysis-only PnL comparison for CP9_MAE20_NEG post-failure cooldowns.

The prior selective-loss study found that CP9_MAE20_NEG improved the exact
baseline trades on both Aug-10 and Aug-11 without flipping a winner, but the
realistic Aug-11 replay deteriorated after the earlier exit freed the symbol for
an additional entry.

This script isolates the post-failure re-entry policy while keeping the current
3-minute PAPER strategy unchanged otherwise.

Modes
-----
BASELINE
    Current 3-minute baseline. No CP9 exit.

CP9_NO_CD
    CP9_MAE20_NEG one-shot exit, with immediate re-entry eligibility.

CP9_CD30
    After a CP9_MAE20_NEG exit, block that symbol for 30 minutes.

CP9_CD60
    After a CP9_MAE20_NEG exit, block that symbol for 60 minutes.

CP9_EOD
    After a CP9_MAE20_NEG exit, block that symbol for the rest of the session.

Only CP9-triggered exits start the cooldown. Ordinary losses, emergency stops,
MAE exits, MFE exits, targets and square-off do not start this new cooldown.

Held constant
-------------
- current 3-minute entry timing/direction
- stored 15m ADX: <20 BLOCK, 20-<40 REVERSE, >=40 NORMAL
- RSI override after ADX gate
- 0.20% risk/trade, 0.45% sizing geometry
- 5% sticky daily-loss halt and aggregate-risk admission guard
- 0.75% emergency stop
- hybrid / native MAE / MFE / dead-trade / square-off exits
- existing transaction-cost model
- same historical source opportunities

Data integrity is fail-closed via replay_failed_development_variants_20260810_11.
This file never imports/runs main.py and cannot place orders.
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pandas as pd

import replay_failed_development_variants_20260810_11 as fd
import replay_selective_loss_checkpoints_20260810_11 as sel

base = fd.base
DATES = ("2026-08-11", "2026-08-10")
CANDIDATE = sel.CANDIDATE_BY_NAME["CP9_MAE20_NEG"]

BASELINE = "BASELINE"
CP9_NO_CD = "CP9_NO_CD"
CP9_CD30 = "CP9_CD30"
CP9_CD60 = "CP9_CD60"
CP9_EOD = "CP9_EOD"
MODES = (BASELINE, CP9_NO_CD, CP9_CD30, CP9_CD60, CP9_EOD)

COOLDOWN_MINUTES = {
    CP9_NO_CD: 0.0,
    CP9_CD30: 30.0,
    CP9_CD60: 60.0,
    CP9_EOD: math.inf,
}

OUT_SUMMARY = Path("/tmp/cp9_mae20_cooldown_summary.csv")
OUT_TRADES = Path("/tmp/cp9_mae20_cooldown_trades.csv")


def post_failure_blocked(mode: str, now, prior_trigger_exit_time):
    """Return (blocked, remaining_minutes_or_inf).

    Boundaries are intentional: an entry exactly 30/60 minutes after the CP9
    exit is allowed. EOD mode blocks every later same-day entry.
    """
    if mode == BASELINE or prior_trigger_exit_time is None:
        return False, 0.0
    minutes = COOLDOWN_MINUTES[mode]
    if minutes == 0.0:
        return False, 0.0
    elapsed = max(
        0.0,
        (pd.Timestamp(now) - pd.Timestamp(prior_trigger_exit_time)).total_seconds() / 60.0,
    )
    if math.isinf(minutes):
        return True, math.inf
    remaining = minutes - elapsed
    return remaining > 1e-9, max(0.0, remaining)


def latest_cp9_exit_for_symbol(accepted, symbol, now):
    exits = []
    for trade in accepted:
        if trade["symbol"] != symbol:
            continue
        replay = trade["replay"]
        if not replay.get("selective_trigger"):
            continue
        exit_time = replay.get("exit_time")
        if exit_time is not None and exit_time <= now:
            exits.append(exit_time)
    return max(exits) if exits else None


def replay_mode(date, mode, opportunities, minute, three, square_off):
    if mode == BASELINE:
        raw = fd.replay_policy(
            date,
            fd.BASELINE,
            opportunities,
            minute,
            three,
            square_off,
        )
        result = sel.baseline_as_summary(date, raw)
        result.update({
            "mode": BASELINE,
            "cooldown_blocks": 0,
            "cooldown_block_detail": [],
        })
        return result

    enriched = fd.enrich_current_direction(opportunities, three)
    accepted = []
    rejected = []
    sticky_halt_time = None
    cooldown_detail = []

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

        # Existing same-symbol-open rule retains priority.
        open_now = fd.open_trades_at(accepted, now)
        if any(t["symbol"] == op["symbol"] for t in open_now):
            rejected.append((op, "SYMBOL_ALREADY_OPEN"))
            continue

        # New targeted post-failure cooldown. Only a prior CP9 exit in this
        # symbol can create this block.
        prior_cp9_exit = latest_cp9_exit_for_symbol(accepted, op["symbol"], now)
        blocked, remaining = post_failure_blocked(mode, now, prior_cp9_exit)
        if blocked:
            reason = (
                "CP9_POST_FAILURE_EOD_LOCK"
                if mode == CP9_EOD
                else f"CP9_POST_FAILURE_{int(COOLDOWN_MINUTES[mode])}M_COOLDOWN"
            )
            rejected.append((op, reason))
            cooldown_detail.append(
                {
                    "time": now,
                    "symbol": op["symbol"],
                    "direction": direction,
                    "prior_cp9_exit": prior_cp9_exit,
                    "remaining_minutes": remaining,
                    "reason": reason,
                }
            )
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
            raise RuntimeError(f"DATA INTEGRITY FAILURE {date} {mode} {op['symbol']}")

        replay = sel.replay_trade_selective(
            op,
            direction,
            qty,
            df1,
            df3,
            CANDIDATE,
            square_off,
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

    result = sel.summarize_trades(date, "REALISTIC_REENTRY", mode, accepted, rejected)
    result.update(
        {
            "mode": mode,
            "cooldown_blocks": len(cooldown_detail),
            "cooldown_block_detail": cooldown_detail,
        }
    )
    return result


def print_result(result, baseline_net=None):
    print("\n" + "=" * 124)
    print(f"{result['date']} | {result['mode']}")
    print("=" * 124)
    print(f"TRADES          : {result['trade_count']}")
    print(
        f"W/L/F           : {result['wins']} / {result['losses']} / {result['flats']} "
        f"| WR {result['win_rate']:.2f}%"
    )
    print(f"GROSS           : Rs {result['gross']:.2f}")
    print(f"COSTS           : Rs {result['costs']:.2f}")
    print(f"NET             : Rs {result['net']:.2f}")
    print(f"RETURN          : {result['return_pct']:.3f}%")
    print(f"MAX DD          : Rs {result['max_dd']:.2f}")
    print(f"CP9 EXITS       : {result['trigger_count']}")
    print(f"COOLDOWN BLOCKS : {result['cooldown_blocks']}")
    if baseline_net is not None:
        print(f"VS BASELINE     : Rs {result['net'] - baseline_net:+.2f}")
    if result.get("rejections"):
        print("REJECTIONS:")
        for reason, count in result["rejections"].most_common():
            print(f"  {reason:<42} {count}")

    if result["cooldown_block_detail"]:
        print("COOLDOWN BLOCK DETAIL")
        print(
            f"{'TIME':<9} {'SYMBOL':<13} {'DIR':>5} {'CP9_EXIT':<9} "
            f"{'REMAIN':>9} REASON"
        )
        for item in result["cooldown_block_detail"]:
            remain = (
                "EOD"
                if math.isinf(item["remaining_minutes"])
                else f"{item['remaining_minutes']:.1f}m"
            )
            print(
                f"{item['time'].strftime('%H:%M:%S'):<9} {item['symbol']:<13} "
                f"{item['direction']:>5} {item['prior_cp9_exit'].strftime('%H:%M:%S'):<9} "
                f"{remain:>9} {item['reason']}"
            )


def row_for_csv(result):
    return {
        "date": result["date"],
        "mode": result["mode"],
        "trades": result["trade_count"],
        "wins": result["wins"],
        "losses": result["losses"],
        "flats": result["flats"],
        "win_rate_pct": result["win_rate"],
        "gross": result["gross"],
        "costs": result["costs"],
        "net": result["net"],
        "return_pct": result["return_pct"],
        "max_dd": result["max_dd"],
        "cp9_exits": result["trigger_count"],
        "cooldown_blocks": result["cooldown_blocks"],
    }


def trade_rows_for_csv(result):
    rows = []
    for trade in result["trades"]:
        replay = trade["replay"]
        rows.append(
            {
                "date": result["date"],
                "mode": result["mode"],
                "entry_time": trade["entry_time"],
                "symbol": trade["symbol"],
                "direction": trade["direction"],
                "adx": trade.get("adx"),
                "qty": trade["qty"],
                "net": replay["net"],
                "gross": replay["gross"],
                "costs": replay["costs"],
                "exit_time": replay["exit_time"],
                "exit_reasons": replay["reasons"],
                "cp9_trigger": replay.get("selective_trigger"),
                "trigger_age": replay.get("trigger_age"),
                "trigger_mfe": replay.get("trigger_mfe"),
                "trigger_mae": replay.get("trigger_mae"),
                "trigger_current": replay.get("trigger_current"),
            }
        )
    for item in result["cooldown_block_detail"]:
        rows.append(
            {
                "date": result["date"],
                "mode": result["mode"],
                "entry_time": item["time"],
                "symbol": item["symbol"],
                "direction": item["direction"],
                "exit_reasons": "BLOCKED_" + item["reason"],
                "prior_cp9_exit": item["prior_cp9_exit"],
                "cooldown_remaining_minutes": item["remaining_minutes"],
            }
        )
    return rows


def main():
    print("Connecting to Kite for ANALYSIS-ONLY CP9 cooldown PnL study...")
    kite = base.get_kite_client()
    all_results = []
    all_trade_rows = []
    by_date = {}

    for date in DATES:
        ctx = fd.configure_date(date)
        opportunities = base.group_history()
        if not opportunities:
            raise SystemExit(f"No source opportunities for {date}")
        symbols = sorted({op["symbol"] for op in opportunities})
        print(f"\nDAY {date} | source={len(opportunities)} | symbols={len(symbols)}")
        minute, three = fd.fetch_market_data_reliable(kite, symbols, date)

        results = {}
        for mode in MODES:
            results[mode] = replay_mode(
                date,
                mode,
                opportunities,
                minute,
                three,
                ctx.square_off,
            )
        by_date[date] = results

        baseline_net = results[BASELINE]["net"]
        for mode in MODES:
            print_result(
                results[mode],
                None if mode == BASELINE else baseline_net,
            )
            all_results.append(row_for_csv(results[mode]))
            all_trade_rows.extend(trade_rows_for_csv(results[mode]))

    print("\n" + "#" * 124)
    print("TWO-DAY CP9_MAE20 POST-FAILURE COOLDOWN PNL COMPARISON")
    print("#" * 124)
    print(
        f"{'MODE':<14} {'AUG11':>10} {'AUG10':>10} {'2DAY NET':>12} "
        f"{'VS BASE':>10} {'TRADES':>8} {'WR':>8} {'MAXDD11':>10} {'CD BLOCKS':>10}"
    )

    combined = {}
    baseline_2d = sum(by_date[d][BASELINE]["net"] for d in DATES)
    for mode in MODES:
        r11 = by_date["2026-08-11"][mode]
        r10 = by_date["2026-08-10"][mode]
        net2d = r11["net"] + r10["net"]
        trades = r11["trade_count"] + r10["trade_count"]
        wins = r11["wins"] + r10["wins"]
        wr = wins / trades * 100.0 if trades else 0.0
        blocks = r11["cooldown_blocks"] + r10["cooldown_blocks"]
        combined[mode] = net2d
        print(
            f"{mode:<14} {r11['net']:>10.2f} {r10['net']:>10.2f} {net2d:>12.2f} "
            f"{net2d - baseline_2d:>+10.2f} {trades:>8} {wr:>7.2f}% "
            f"{r11['max_dd']:>10.2f} {blocks:>10}"
        )

    best_mode = max(MODES, key=lambda m: combined[m])
    print(f"\nBASELINE TWO-DAY NET : Rs {baseline_2d:.2f}")
    print(f"BEST MODE            : {best_mode}")
    print(f"BEST TWO-DAY NET     : Rs {combined[best_mode]:.2f}")
    print(f"IMPROVEMENT          : Rs {combined[best_mode] - baseline_2d:+.2f}")

    # A deployment candidate must beat baseline on each day, not merely in the
    # combined sum. This prevents one strong day from masking deterioration.
    if best_mode != BASELINE:
        beats_both = all(
            by_date[d][best_mode]["net"] > by_date[d][BASELINE]["net"] + 1e-9
            for d in DATES
        )
    else:
        beats_both = False
    print(f"BEATS BASELINE BOTH DAYS: {'YES' if beats_both else 'NO'}")
    print(
        "RESEARCH DECISION     : "
        + (
            f"{best_mode} is the leading PAPER candidate; deployment still requires explicit approval."
            if beats_both
            else "keep current 3-minute baseline; no cooldown variant clears the strict gate."
        )
    )

    pd.DataFrame(all_results).to_csv(OUT_SUMMARY, index=False)
    pd.DataFrame(all_trade_rows).to_csv(OUT_TRADES, index=False)
    print("\nFILES")
    print(f"  {OUT_SUMMARY}")
    print(f"  {OUT_TRADES}")
    print("DEPLOYMENT: NONE — ANALYSIS ONLY")


if __name__ == "__main__":
    main()
