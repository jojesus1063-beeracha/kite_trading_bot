#!/usr/bin/env python3
"""Replay all 11-Aug-2026 executed opportunities with ADX direction only.

Analysis only: never imports/runs main.py and never places orders.

Policy:
- capital from config (expected Rs 5,000)
- risk/trade 0.20%
- ADX <20 => reversed EMA9/EMA21
- ADX >=20 => normal EMA9/EMA21
- RSI >=70 BUY override; RSI <=30 SELL override
- NO ADX entry block
- NO max entries/day frequency cap
- NO per-symbol completed-entry cap
- NO loss cooldown
- NO max-open-position cap
- concurrent same-symbol historical opportunities are replayed independently
- daily realized-loss halt remains 5% of capital
- 0.75% PAPER emergency stop
- current revised MAE/MFE/hybrid/square-off logic reused from
  replay_proposed_adx_20260811.py

The only non-policy rejection that can still occur is zero quantity when the
0.20% risk budget cannot fund even one share at the 0.45% strategy-stop sizing
distance, or missing historical data/direction.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd

import replay_proposed_adx_20260811 as base

RISK_PER_TRADE_PCT = 0.20
RISK_AMOUNT = base.CAPITAL * RISK_PER_TRADE_PCT / 100.0
DAILY_LOSS_LIMIT = base.CAPITAL * 5.0 / 100.0


def main():
    opportunities = base.group_history()
    if not opportunities:
        raise SystemExit("No 11-Aug-2026 trade history found")

    symbols = sorted({x["symbol"] for x in opportunities})
    print("Connecting to Kite...")
    kite = base.get_kite_client()
    minute, three = base.fetch_market_data(kite, symbols)

    enriched = []
    reconstruction_mismatches = []
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
            reconstruction_mismatches.append(op)
        enriched.append(op)

    accepted = []
    rejected = []
    halt_time = None

    for op in enriched:
        now = op["order_time"]

        # Daily kill switch is the only session-level entry blocker retained.
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
    wins = sum(1 for x in accepted if x["replay"]["net"] > 0)
    losses = sum(1 for x in accepted if x["replay"]["net"] < 0)
    flat = len(accepted) - wins - losses
    changed = sum(
        1 for x in accepted if x["direction"] != x["actual_direction"]
    )

    print("\n" + "=" * 168)
    print("11 AUG 2026 — ADX DIRECTION-ONLY / ALL-ENTRY REPLAY")
    print(
        "ADX <20 REVERSE | ADX >=20 NORMAL | NO ADX/FREQUENCY/OPEN-POSITION "
        "ENTRY BLOCKS | risk 0.20% | emergency stop 0.75%"
    )
    print("=" * 168)
    print(
        f"{'#':<3} {'SYMBOL':<13} {'ADX':>6} {'OLD':>5} {'NEW':>5} "
        f"{'RSI':>7} {'QTY':>4} {'ENTRY':>9} {'ACTUAL':>10} {'REPLAY':>10} "
        f"{'DELTA':>10} {'MFE%':>7} {'MAE%':>7} EXIT"
    )
    print("-" * 168)

    for idx, x in enumerate(accepted, start=1):
        r = x["replay"]
        delta = r["net"] - x["actual_net"]
        rsi_text = "NA" if x.get("rsi") is None else f"{x['rsi']:.1f}"
        adx_text = "NA" if x.get("adx") is None else f"{x['adx']:.2f}"
        print(
            f"{idx:<3} {x['symbol']:<13} {adx_text:>6} "
            f"{x['actual_direction']:>5} {x['direction']:>5} {rsi_text:>7} "
            f"{x['qty']:>4} {x['entry']:>9.2f} {x['actual_net']:>10.2f} "
            f"{r['net']:>10.2f} {delta:>+10.2f} {r['mfe']:>7.3f} "
            f"{r['mae']:>7.3f} {r['reasons']}"
        )

    print("=" * 168)
    print(f"SOURCE UNIQUE EXECUTED OPPORTUNITIES : {len(opportunities)}")
    print(f"ACCEPTED / SIZEABLE AT 0.20% RISK    : {len(accepted)}")
    print(f"NOT TRADED                           : {len(rejected)}")
    print(f"DIRECTION CHANGED                    : {changed}")
    print(f"WINS                                 : {wins}")
    print(f"LOSSES                               : {losses}")
    print(f"FLAT                                 : {flat}")
    print(
        f"WIN RATE                             : "
        f"{(wins / len(accepted) * 100 if accepted else 0):.2f}%"
    )
    print()
    print(f"RISK BUDGET PER TRADE                : Rs {RISK_AMOUNT:.2f}")
    print(f"REPLAY GROSS P&L                     : Rs {gross:.2f}")
    print(f"REPLAY COSTS                         : Rs {costs:.2f}")
    print(f"REPLAY NET P&L                       : Rs {net:.2f}")
    print(f"REPLAY RETURN                        : {(net / base.CAPITAL * 100):.3f}%")
    print(f"VS ACTUAL DAY NET (-372.11)          : Rs {net - (-372.11):+.2f}")
    print(f"5% DAILY-LOSS HALT THRESHOLD         : Rs {-DAILY_LOSS_LIMIT:.2f}")
    print(
        f"HALT WOULD HAVE TRIGGERED            : "
        f"{'YES at ' + str(eod_halt_time) if eod_halt_time is not None else 'NO'}"
    )

    exit_dist = Counter()
    exit_net = defaultdict(float)
    for x in accepted:
        for leg in x["replay"]["legs"]:
            exit_dist[leg["reason"]] += 1
            exit_net[leg["reason"]] += leg["net"]

    print("\n" + "=" * 100)
    print("REPLAY EXIT DISTRIBUTION")
    print("=" * 100)
    for reason, count in exit_dist.most_common():
        print(f"{reason:<38} count={count:<3} net=Rs {exit_net[reason]:>10.2f}")

    print("\n" + "=" * 100)
    print("NOT-TRADED REASONS")
    print("=" * 100)
    rejection_counts = Counter(reason for _, reason in rejected)
    for reason, count in rejection_counts.most_common():
        print(f"{reason:<38} count={count}")
    for op, reason in rejected:
        adx_text = "NA" if op.get("adx") is None else f"{op['adx']:.2f}"
        print(
            f"{op['order_time'].strftime('%H:%M:%S')} {op['symbol']:<13} "
            f"ADX={adx_text:>6} old={op['actual_direction']:<4} -> {reason}"
        )

    print("\n" + "=" * 100)
    print("ENTRY-DIRECTION RECONSTRUCTION CHECK")
    print("=" * 100)
    print(
        f"Historical old-policy direction mismatches vs actual: "
        f"{len(reconstruction_mismatches)} / {len(opportunities)}"
    )
    print("NOTE: same-executed-opportunity counterfactual, not a full alternate-history rescan.")


if __name__ == "__main__":
    main()
