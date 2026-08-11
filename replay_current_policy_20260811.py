#!/usr/bin/env python3
"""Replay 11-Aug-2026 using the CURRENT systemd PAPER policy.

Analysis only. This script never imports/runs main.py and never places orders.

Current policy mirrored here:
- capital from config (expected Rs 5,000)
- risk/trade 0.20%
- ADX <20: BLOCK
- 20 <= ADX <40: REVERSED EMA9/EMA21
- ADX >=40: NORMAL EMA9/EMA21
- RSI >=70 BUY override; RSI <=30 SELL override, only after ADX gate
- no 14:00 cutoff
- no consecutive-loss symbol guard
- no early-failure experiment
- no daily entry-count / per-symbol completed-count / cooldown caps
- same-symbol concurrent position blocked (runtime open_positions is symbol-keyed)
- 5% sticky realized daily-loss halt
- aggregate admission guard:
    realized_loss + open strategy risk + proposed strategy risk < daily budget
  Exact equality blocks.
- sizing/open-risk geometry 0.45%
- executable PAPER emergency stop 0.75%
- existing MAE/MFE/hybrid/dead-trade/square-off replay retained

This remains a same-executed-opportunity counterfactual: it reuses source
opportunities from trade_history.jsonl and does not rescan the whole market.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

import pandas as pd

import replay_direction_only_two_days_20260810_11 as harness

base = harness.base
SESSION_DATE = "2026-08-11"
RISK_PER_TRADE_PCT = 0.20
RISK_AMOUNT = base.CAPITAL * RISK_PER_TRADE_PCT / 100.0
DAILY_LOSS_LIMIT = base.CAPITAL * 5.0 / 100.0
STRATEGY_STOP_PCT = base.STRATEGY_STOP_PCT


def current_direction(adx, ema9, ema21, rsi):
    """Return (final, base, RSI override), or BLOCK for ADX<20."""
    if adx is not None and math.isfinite(float(adx)) and float(adx) < 20.0:
        return None, None, None

    normal = adx is not None and math.isfinite(float(adx)) and float(adx) >= 40.0
    if ema9 > ema21:
        base_direction = "BUY" if normal else "SELL"
    elif ema9 < ema21:
        base_direction = "SELL" if normal else "BUY"
    else:
        return None, None, None

    override = None
    if rsi is not None and math.isfinite(float(rsi)):
        if float(rsi) >= base.RSI_OVERBOUGHT:
            override = "BUY"
        elif float(rsi) <= base.RSI_OVERSOLD:
            override = "SELL"
    return override or base_direction, base_direction, override


def configure_date():
    base.SESSION_DATE = SESSION_DATE
    base.SQUARE_OFF = pd.Timestamp(
        f"{SESSION_DATE} {getattr(base.cfg, 'FORCE_SQUARE_OFF_TIME', '15:08')}",
        tz=base.IST,
    )


def enrich(opportunities, three):
    enriched = []
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
        final, base_direction, override = current_direction(op.get("adx"), ema9, ema21, rsi)
        op.update({
            "ema9": ema9,
            "ema21": ema21,
            "rsi": rsi,
            "proposed_direction": final,
            "proposed_base": base_direction,
            "rsi_override": override,
        })
        enriched.append(op)
    return enriched


def completed_legs(accepted, now):
    return sorted(
        [
            leg
            for trade in accepted
            for leg in trade["replay"]["legs"]
            if leg["time"] <= now
        ],
        key=lambda leg: leg["time"],
    )


def realized_pnl_at(accepted, now):
    return sum(float(leg["net"]) for leg in completed_legs(accepted, now))


def remaining_qty_at(trade, now):
    closed = sum(
        int(leg["qty"])
        for leg in trade["replay"]["legs"]
        if leg["time"] <= now
    )
    return max(0, int(trade["qty"]) - closed)


def open_trades_at(accepted, now):
    return [
        trade
        for trade in accepted
        if trade["entry_time"] <= now and remaining_qty_at(trade, now) > 0
    ]


def strategy_risk_for(entry, qty):
    return float(entry) * STRATEGY_STOP_PCT / 100.0 * int(qty)


def replay_day(kite):
    configure_date()
    opportunities = base.group_history()
    if not opportunities:
        raise SystemExit(f"No trade-history opportunities found for {SESSION_DATE}")

    symbols = sorted({op["symbol"] for op in opportunities})
    print(f"Connecting to Kite history | {SESSION_DATE} | symbols={len(symbols)}")
    minute, three = base.fetch_market_data(kite, symbols)
    enriched = enrich(opportunities, three)

    accepted = []
    rejected = []
    sticky_halt_time = None

    for op in enriched:
        now = op["order_time"]
        realized = realized_pnl_at(accepted, now)

        # Sticky daily halt: once crossed, it never clears later that day.
        if sticky_halt_time is None:
            running = 0.0
            for leg in completed_legs(accepted, now):
                running += float(leg["net"])
                if running <= -DAILY_LOSS_LIMIT:
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

        per_share_risk = float(op["entry"]) * STRATEGY_STOP_PCT / 100.0
        risk_qty = int(RISK_AMOUNT / per_share_risk) if per_share_risk > 0 else 0
        qty = min(risk_qty, int(op["actual_qty"]))
        if qty <= 0:
            rejected.append((op, "QTY_ZERO_AT_0_2PCT_RISK"))
            continue

        open_now = open_trades_at(accepted, now)
        if any(trade["symbol"] == op["symbol"] for trade in open_now):
            rejected.append((op, "SYMBOL_ALREADY_OPEN"))
            continue

        # Mirror paper_daily_risk_guard: negative realized P&L only contributes
        # to used daily risk. Existing open risk uses remaining quantity and the
        # 0.45% sizing geometry; proposed risk uses the same geometry.
        realized_loss = max(0.0, -realized)
        open_risk = sum(
            strategy_risk_for(trade["entry"], remaining_qty_at(trade, now))
            for trade in open_now
        )
        proposed_risk = strategy_risk_for(op["entry"], qty)
        aggregate = realized_loss + open_risk + proposed_risk
        if aggregate >= DAILY_LOSS_LIMIT:
            op["aggregate_risk_detail"] = {
                "realized_pnl": realized,
                "realized_loss": realized_loss,
                "open_risk": open_risk,
                "proposed_risk": proposed_risk,
                "aggregate": aggregate,
                "budget": DAILY_LOSS_LIMIT,
            }
            rejected.append((op, "AGGREGATE_DAILY_RISK_GTE_BUDGET"))
            continue

        df1 = minute.get(op["symbol"])
        df3 = three.get(op["symbol"])
        if df1 is None or df1.empty or df3 is None or df3.empty:
            rejected.append((op, "MISSING_HISTORY"))
            continue

        result = base.replay_trade(op, direction, qty, df1, df3)
        if result is None:
            rejected.append((op, "NO_EXIT_HISTORY"))
            continue

        accepted.append({
            **op,
            "direction": direction,
            "qty": qty,
            "risk_qty": risk_qty,
            "entry": float(op["entry"]),
            "entry_time": now,
            "replay": result,
            "admission": {
                "realized_pnl": realized,
                "realized_loss": realized_loss,
                "open_risk": open_risk,
                "proposed_risk": proposed_risk,
                "aggregate": aggregate,
                "budget": DAILY_LOSS_LIMIT,
            },
        })

    legs = sorted(
        [leg for trade in accepted for leg in trade["replay"]["legs"]],
        key=lambda leg: leg["time"],
    )
    running = 0.0
    eod_halt = sticky_halt_time
    max_realized_drawdown = 0.0
    for leg in legs:
        running += float(leg["net"])
        max_realized_drawdown = min(max_realized_drawdown, running)
        if eod_halt is None and running <= -DAILY_LOSS_LIMIT:
            eod_halt = leg["time"]

    gross = sum(float(x["replay"]["gross"]) for x in accepted)
    costs = sum(float(x["replay"]["costs"]) for x in accepted)
    net = sum(float(x["replay"]["net"]) for x in accepted)
    wins = sum(1 for x in accepted if x["replay"]["net"] > 0)
    losses = sum(1 for x in accepted if x["replay"]["net"] < 0)
    flat = len(accepted) - wins - losses
    actual_net = sum(float(x["actual_net"]) for x in opportunities)

    rejection_counts = Counter(reason for _, reason in rejected)
    exit_counts = Counter()
    exit_net = defaultdict(float)
    for trade in accepted:
        for leg in trade["replay"]["legs"]:
            exit_counts[leg["reason"]] += 1
            exit_net[leg["reason"]] += float(leg["net"])

    adx_band = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
    for trade in accepted:
        adx = trade.get("adx")
        if adx is None:
            band = "ADX_NA"
        elif adx < 30:
            band = "20-<30"
        elif adx < 40:
            band = "30-<40"
        else:
            band = ">=40"
        adx_band[band]["trades"] += 1
        adx_band[band]["wins"] += int(trade["replay"]["net"] > 0)
        adx_band[band]["net"] += float(trade["replay"]["net"])

    print("\n" + "=" * 118)
    print("11-AUG-2026 CURRENT PAPER POLICY — PROFIT STATEMENT")
    print("ADX<20 BLOCK | 20-<40 REVERSE | >=40 NORMAL | no 14:00 cutoff | aggregate risk guard ON")
    print("=" * 118)
    print(f"SOURCE OPPORTUNITIES      : {len(opportunities)}")
    print(f"TRADES TAKEN              : {len(accepted)}")
    print(f"NOT TRADED                : {len(rejected)}")
    print(f"WINS / LOSSES / FLAT      : {wins} / {losses} / {flat}")
    print(f"WIN RATE                  : {(wins / len(accepted) * 100 if accepted else 0):.2f}%")
    print(f"REPLAY GROSS              : Rs {gross:.2f}")
    print(f"ESTIMATED COSTS           : Rs {costs:.2f}")
    print(f"REPLAY NET                : Rs {net:.2f}")
    print(f"RETURN ON Rs {base.CAPITAL:.0f}       : {net / base.CAPITAL * 100.0:.3f}%")
    print(f"ACTUAL AUG-11 NET         : Rs {actual_net:.2f}")
    print(f"IMPROVEMENT VS ACTUAL     : Rs {net - actual_net:+.2f}")
    print(f"MAX REALIZED DRAWDOWN     : Rs {max_realized_drawdown:.2f}")
    print(f"DAILY LOSS BUDGET         : Rs {DAILY_LOSS_LIMIT:.2f}")
    print(f"DAILY HALT TRIGGERED      : {'YES ' + str(eod_halt) if eod_halt is not None else 'NO'}")

    print("\nNOT-TRADED REASONS")
    for reason, count in rejection_counts.most_common():
        print(f"  {reason:<38} {count}")

    print("\nADX BAND PERFORMANCE")
    for band in ("20-<30", "30-<40", ">=40", "ADX_NA"):
        item = adx_band.get(band)
        if not item:
            continue
        wr = item["wins"] / item["trades"] * 100.0 if item["trades"] else 0.0
        print(f"  {band:<8} trades={item['trades']:<3} wins={item['wins']:<3} WR={wr:>6.2f}% net=Rs {item['net']:>9.2f}")

    print("\nEXIT DISTRIBUTION")
    for reason, count in exit_counts.most_common():
        print(f"  {reason:<36} count={count:<3} net=Rs {exit_net[reason]:>9.2f}")

    print("\nTRADE DETAIL")
    print(f"{'TIME':<9} {'SYMBOL':<13} {'ADX':>6} {'DIR':>5} {'QTY':>4} {'NET':>9} {'MFE%':>7} {'MAE%':>7} {'AGG_RISK':>9} EXIT")
    for trade in accepted:
        r = trade["replay"]
        adx_text = "NA" if trade.get("adx") is None else f"{trade['adx']:.2f}"
        print(
            f"{trade['order_time'].strftime('%H:%M:%S'):<9} {trade['symbol']:<13} {adx_text:>6} "
            f"{trade['direction']:>5} {trade['qty']:>4} {r['net']:>9.2f} {r['mfe']:>7.3f} {r['mae']:>7.3f} "
            f"{trade['admission']['aggregate']:>9.2f} {r['reasons']}"
        )

    if any(reason == "AGGREGATE_DAILY_RISK_GTE_BUDGET" for _, reason in rejected):
        print("\nAGGREGATE-RISK BLOCK DETAIL")
        for op, reason in rejected:
            if reason != "AGGREGATE_DAILY_RISK_GTE_BUDGET":
                continue
            d = op["aggregate_risk_detail"]
            print(
                f"  {op['order_time'].strftime('%H:%M:%S')} {op['symbol']:<13} "
                f"realized={d['realized_pnl']:.2f} openRisk={d['open_risk']:.2f} "
                f"proposed={d['proposed_risk']:.2f} aggregate={d['aggregate']:.2f} budget={d['budget']:.2f}"
            )

    return net


def main():
    kite = base.get_kite_client()
    replay_day(kite)


if __name__ == "__main__":
    main()
