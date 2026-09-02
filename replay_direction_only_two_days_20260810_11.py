#!/usr/bin/env python3
"""Replay 10-Aug-2026 and 11-Aug-2026 with the verified PAPER policy.

Analysis only. This script never imports/runs main.py and cannot place orders.

Policy under test:
- capital from config (expected Rs 5,000)
- risk/trade 0.20%
- ADX <20 => reversed EMA9/EMA21
- ADX >=20 => normal EMA9/EMA21
- RSI >=70 BUY override; RSI <=30 SELL override
- no ADX entry block
- no daily entry-count cap
- no completed-trades-per-symbol cap
- no post-loss cooldown
- no max-open-position frequency cap
- same-symbol historical opportunities replay independently
- 5% realized daily-loss halt remains active
- 0.75% PAPER emergency stop
- revised MAE/MFE/hybrid exits and 40-minute dead-trade exit
- 15:08 square-off

This is a same-executed-opportunity counterfactual. It reuses the opportunities
that actually existed in trade_history.jsonl for each date; it is not a full
alternate-history rescan of every candidate that might have become available.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd

import replay_proposed_adx_20260811 as base

DATES = ["2026-08-10", "2026-08-11"]
RISK_PER_TRADE_PCT = 0.20
RISK_AMOUNT = base.CAPITAL * RISK_PER_TRADE_PCT / 100.0
DAILY_LOSS_LIMIT = base.CAPITAL * 5.0 / 100.0


def configure_date(session_date: str) -> None:
    base.SESSION_DATE = session_date
    base.SQUARE_OFF = pd.Timestamp(
        f"{session_date} {getattr(base.cfg, 'FORCE_SQUARE_OFF_TIME', '15:08')}",
        tz=base.IST,
    )


def enrich_opportunities(opportunities, three):
    enriched = []
    mismatches = []
    for op in opportunities:
        df3 = three.get(op["symbol"])
        row = base.indicator_row(df3, op["signal_start"])
        if row is None:
            op["indicator_error"] = "missing 3-minute indicator row"
            enriched.append(op)
            continue

        ema9 = float(row["ema9"])
        ema21 = float(row["ema21"])
        rsi = float(row["rsi14"]) if not pd.isna(row["rsi14"]) else None
        proposed, base_direction, override = base.proposed_direction(
            op["adx"], ema9, ema21, rsi
        )
        reconstructed_current = base.current_policy_direction(
            op["adx"], ema9, ema21, rsi
        )
        op.update({
            "ema9": ema9,
            "ema21": ema21,
            "rsi": rsi,
            "proposed_direction": proposed,
            "proposed_base": base_direction,
            "rsi_override": override,
            "reconstructed_current": reconstructed_current,
        })
        if reconstructed_current and reconstructed_current != op["actual_direction"]:
            mismatches.append(op)
        enriched.append(op)
    return enriched, mismatches


def replay_day(session_date: str, kite):
    configure_date(session_date)
    opportunities = base.group_history()
    if not opportunities:
        return {
            "date": session_date,
            "error": f"No trade-history opportunities found for {session_date}",
        }

    symbols = sorted({x["symbol"] for x in opportunities})
    print(f"\nFetching Kite history for {session_date} | symbols={len(symbols)}")
    minute, three = base.fetch_market_data(kite, symbols)
    enriched, mismatches = enrich_opportunities(opportunities, three)

    accepted = []
    rejected = []
    halt_time = None

    for op in enriched:
        now = op["order_time"]

        # The 5% daily realized-loss halt is the only retained session-level
        # blocker. All requested frequency/ADX blockers are intentionally off.
        prior_legs = sorted(
            [
                leg
                for trade in accepted
                for leg in trade["replay"]["legs"]
                if leg["time"] <= now
            ],
            key=lambda x: x["time"],
        )
        running = 0.0
        for leg in prior_legs:
            running += leg["net"]
            if running <= -DAILY_LOSS_LIMIT:
                if halt_time is None:
                    halt_time = leg["time"]
                break
        if halt_time is not None and now >= halt_time:
            rejected.append((op, "DAILY_LOSS_5PCT_HALT"))
            continue

        direction = op.get("proposed_direction")
        if direction not in {"BUY", "SELL"}:
            rejected.append((op, "DIRECTION_UNAVAILABLE"))
            continue

        per_share_risk = op["entry"] * base.STRATEGY_STOP_PCT / 100.0
        risk_qty = int(RISK_AMOUNT / per_share_risk) if per_share_risk > 0 else 0
        qty = min(risk_qty, op["actual_qty"])
        if qty <= 0:
            rejected.append((op, "QTY_ZERO_AT_0_2PCT_RISK"))
            continue

        df1 = minute.get(op["symbol"])
        df3 = three.get(op["symbol"])
        if df1 is None or df1.empty or df3 is None or df3.empty:
            rejected.append((op, "MISSING_HISTORY"))
            continue

        replay = base.replay_trade(op, direction, qty, df1, df3)
        if replay is None:
            rejected.append((op, "NO_EXIT_HISTORY"))
            continue

        accepted.append({
            **op,
            "direction": direction,
            "qty": qty,
            "risk_qty": risk_qty,
            "entry_time": now,
            "replay": replay,
        })

    all_legs = sorted(
        [leg for x in accepted for leg in x["replay"]["legs"]],
        key=lambda x: x["time"],
    )
    running = 0.0
    eod_halt_time = halt_time
    for leg in all_legs:
        running += leg["net"]
        if eod_halt_time is None and running <= -DAILY_LOSS_LIMIT:
            eod_halt_time = leg["time"]

    net = sum(x["replay"]["net"] for x in accepted)
    gross = sum(x["replay"]["gross"] for x in accepted)
    costs = sum(x["replay"]["costs"] for x in accepted)
    actual_net = sum(x["actual_net"] for x in opportunities)
    wins = sum(1 for x in accepted if x["replay"]["net"] > 0)
    losses = sum(1 for x in accepted if x["replay"]["net"] < 0)
    flat = len(accepted) - wins - losses
    changed = sum(1 for x in accepted if x["direction"] != x["actual_direction"])

    exit_dist = Counter()
    exit_net = defaultdict(float)
    for x in accepted:
        for leg in x["replay"]["legs"]:
            exit_dist[leg["reason"]] += 1
            exit_net[leg["reason"]] += leg["net"]

    rejection_counts = Counter(reason for _, reason in rejected)

    print("\n" + "=" * 132)
    print(f"{session_date} — ADX DIRECTION-ONLY / 0.20% RISK REPLAY")
    print("ADX <20 REVERSE | ADX >=20 NORMAL | no ADX/frequency caps | emergency stop 0.75%")
    print("=" * 132)
    print(f"SOURCE OPPORTUNITIES                 : {len(opportunities)}")
    print(f"ACCEPTED / SIZEABLE                 : {len(accepted)}")
    print(f"NOT TRADED                          : {len(rejected)}")
    print(f"DIRECTION CHANGED                   : {changed}")
    print(f"WINS / LOSSES / FLAT                : {wins} / {losses} / {flat}")
    print(f"WIN RATE                            : {(wins / len(accepted) * 100 if accepted else 0):.2f}%")
    print(f"ACTUAL NET FOR SOURCE OPPORTUNITIES : Rs {actual_net:.2f}")
    print(f"REPLAY GROSS                        : Rs {gross:.2f}")
    print(f"REPLAY COSTS                        : Rs {costs:.2f}")
    print(f"REPLAY NET                          : Rs {net:.2f}")
    print(f"REPLAY RETURN ON CAPITAL            : {(net / base.CAPITAL * 100):.3f}%")
    print(f"CHANGE VS ACTUAL                    : Rs {net - actual_net:+.2f}")
    print(f"5% DAILY LOSS LIMIT                 : Rs {-DAILY_LOSS_LIMIT:.2f}")
    print(
        "HALT WOULD HAVE TRIGGERED           : "
        + (f"YES at {eod_halt_time}" if eod_halt_time is not None else "NO")
    )
    print(f"OLD-DIRECTION RECONSTRUCTION MISMATCHES: {len(mismatches)} / {len(opportunities)}")

    if rejection_counts:
        print("\nNOT-TRADED REASONS")
        for reason, count in rejection_counts.most_common():
            print(f"  {reason:<36} {count}")

    print("\nEXIT DISTRIBUTION")
    for reason, count in exit_dist.most_common():
        print(f"  {reason:<36} count={count:<3} net=Rs {exit_net[reason]:>9.2f}")

    print("\nTRADE DETAIL")
    print(
        f"{'TIME':<9} {'SYMBOL':<13} {'ADX':>6} {'OLD':>5} {'NEW':>5} {'QTY':>4} "
        f"{'ACTUAL':>9} {'REPLAY':>9} {'MFE%':>7} {'MAE%':>7} EXIT"
    )
    for x in accepted:
        r = x["replay"]
        adx_text = "NA" if x.get("adx") is None else f"{x['adx']:.2f}"
        print(
            f"{x['order_time'].strftime('%H:%M:%S'):<9} {x['symbol']:<13} {adx_text:>6} "
            f"{x['actual_direction']:>5} {x['direction']:>5} {x['qty']:>4} "
            f"{x['actual_net']:>9.2f} {r['net']:>9.2f} {r['mfe']:>7.3f} {r['mae']:>7.3f} "
            f"{r['reasons']}"
        )

    return {
        "date": session_date,
        "source": len(opportunities),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "changed": changed,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "actual_net": actual_net,
        "gross": gross,
        "costs": costs,
        "net": net,
        "return_pct": net / base.CAPITAL * 100.0,
        "halt_time": eod_halt_time,
        "rejections": dict(rejection_counts),
        "mismatches": len(mismatches),
    }


def main():
    print("Connecting to Kite...")
    kite = base.get_kite_client()
    results = [replay_day(day, kite) for day in DATES]

    print("\n" + "=" * 132)
    print("TWO-DAY COMPARISON — VERIFIED PAPER POLICY")
    print("=" * 132)
    print(
        f"{'DATE':<12} {'SRC':>5} {'USED':>5} {'W':>4} {'L':>4} {'WIN%':>7} "
        f"{'ACTUAL':>11} {'GROSS':>11} {'COSTS':>10} {'NET':>11} {'RETURN%':>9}"
    )

    total_actual = total_gross = total_costs = total_net = 0.0
    valid_days = 0
    for r in results:
        if r.get("error"):
            print(f"{r['date']:<12} ERROR: {r['error']}")
            continue
        valid_days += 1
        win_rate = r["wins"] / r["accepted"] * 100 if r["accepted"] else 0.0
        print(
            f"{r['date']:<12} {r['source']:>5} {r['accepted']:>5} {r['wins']:>4} {r['losses']:>4} "
            f"{win_rate:>6.2f}% {r['actual_net']:>11.2f} {r['gross']:>11.2f} "
            f"{r['costs']:>10.2f} {r['net']:>11.2f} {r['return_pct']:>8.3f}%"
        )
        total_actual += r["actual_net"]
        total_gross += r["gross"]
        total_costs += r["costs"]
        total_net += r["net"]

    if valid_days:
        print("-" * 132)
        print(f"TWO-DAY ACTUAL NET                  : Rs {total_actual:.2f}")
        print(f"TWO-DAY REPLAY GROSS                : Rs {total_gross:.2f}")
        print(f"TWO-DAY REPLAY COSTS                : Rs {total_costs:.2f}")
        print(f"TWO-DAY REPLAY NET                  : Rs {total_net:.2f}")
        print(f"TWO-DAY CHANGE VS ACTUAL            : Rs {total_net - total_actual:+.2f}")
        print(f"TWO-DAY RETURN ON Rs {base.CAPITAL:.2f} CAPITAL : {(total_net / base.CAPITAL * 100):.3f}%")

    print("\nNOTE: same-executed-opportunity counterfactual; not a full alternate-history rescan.")


if __name__ == "__main__":
    main()
