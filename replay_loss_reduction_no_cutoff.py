#!/usr/bin/env python3
"""Two-day counterfactual replay for the PAPER loss-reduction package.

Dates: 2026-08-10 and 2026-08-11.
Analysis only; never imports/runs main.py and cannot place orders.

Policy:
- ADX <20 BLOCK
- 20<=ADX<40 REVERSE EMA9/EMA21
- ADX>=40 NORMAL EMA9/EMA21
- RSI extremes retain the existing post-ADX-gate override
- no new entries at/after 14:00 IST
- after 2 consecutive completed losses in a symbol, block that symbol for day
- same-symbol concurrent entries blocked to match running position model
- early failure: >10m, MFE<0.15%, current<0, 3 adverse EMA candles
- risk 0.20%, emergency stop 0.75%, existing MAE/MFE/hybrid exits
- daily realized loss halt 5%

Uses the same executed historical opportunity set, so this remains a
same-opportunity counterfactual rather than a full alternate-history rescan.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

import pandas as pd

import replay_direction_only_two_days_20260810_11 as harness

base = harness.base
DATES = ["2026-08-10", "2026-08-11"]
RISK_PER_TRADE_PCT = 0.20
RISK_AMOUNT = base.CAPITAL * RISK_PER_TRADE_PCT / 100.0
DAILY_LOSS_LIMIT = base.CAPITAL * 5.0 / 100.0
ENTRY_CUTOFF = pd.Timestamp("14:00").time()
# Set False (via --no-entry-cutoff) to measure the loss-reduction package
# WITHOUT the blunt 14:00 time ban, isolating the two targeted rules
# (consecutive-loss guard + early low-MFE exit) from it.
ENABLE_ENTRY_CUTOFF = True
EARLY_MIN_AGE = 10.0
EARLY_MAX_MFE = 0.15
CONSECUTIVE_LOSS_BLOCK = 2


def final_direction(adx, ema9, ema21, rsi):
    if adx is not None and math.isfinite(float(adx)) and float(adx) < 20.0:
        return None, None, "ADX_LT20_BLOCK"

    normal = adx is not None and math.isfinite(float(adx)) and float(adx) >= 40.0
    if ema9 > ema21:
        d = "BUY" if normal else "SELL"
    elif ema9 < ema21:
        d = "SELL" if normal else "BUY"
    else:
        return None, None, None

    override = None
    if rsi is not None and math.isfinite(float(rsi)):
        if float(rsi) >= base.RSI_OVERBOUGHT:
            override = "BUY"
        elif float(rsi) <= base.RSI_OVERSOLD:
            override = "SELL"
    return override or d, d, override


def replay_trade(op, direction, qty, df1, df3):
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
        runner_qty = qty - scalp_qty
        scalp_target = entry + sign * strategy_risk * base.SCALP_R
        runner_target = entry + sign * strategy_risk * base.RUNNER_R
    else:
        scalp_qty = 0
        runner_qty = qty
        scalp_target = None
        runner_target = entry + sign * entry * base.NON_HYBRID_TARGET_PCT / 100.0

    rows = df1.loc[
        (df1["date"] >= order_time.floor("min")) & (df1["date"] <= base.SQUARE_OFF)
    ].copy()
    if rows.empty:
        return None

    remaining = qty
    scalp_pending = hybrid
    be_active = False
    legs = []
    max_mfe = 0.0
    min_mae = 0.0

    def close_leg(now, price, amount, reason):
        nonlocal remaining
        amount = min(int(amount), remaining)
        if amount <= 0:
            return
        c = base.cost_leg(direction, amount, entry, price)
        legs.append({"time": now, "qty": amount, "price": float(price), "reason": reason, **c})
        remaining -= amount

    for _, row in rows.iterrows():
        now = row["date"]
        price = float(row["close"])
        since = df1.loc[
            (df1["date"] >= timer_start.floor("min")) & (df1["date"] <= now)
        ]
        mfe, mae, current, giveback = base.excursions(direction, entry, since, price)
        max_mfe = max(max_mfe, mfe)
        min_mae = min(min_mae, mae)
        age = max(0.0, (now - timer_start).total_seconds() / 60.0)

        hit_emergency = price <= emergency_stop if direction == "BUY" else price >= emergency_stop
        if not be_active and hit_emergency:
            close_leg(now, price, remaining, "paper_emergency_stop_0_75")
            break

        if be_active:
            hit_be = price <= entry if direction == "BUY" else price >= entry
            if hit_be:
                close_leg(now, price, remaining, "hybrid_breakeven_stop")
                break

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
                close_leg(now, price, remaining, "hybrid_runner_2r" if hybrid else "fixed_target")
                break

        adverse = False
        if age > base.MAE_MIN_AGE:
            adverse = base.adverse_three(direction, df3, now)
            if (
                mae <= base.MAE_THRESHOLD
                and current <= base.CURRENT_LOSS_THRESHOLD
                and mfe < base.MAX_MFE_FAILURE
                and adverse
            ):
                close_leg(now, price, remaining, "mae_adverse_trend_10m")
                break

        reason = base.mfe_reason(age, mfe, current, giveback)
        if reason:
            close_leg(now, price, remaining, reason)
            break

        # New early-failure overlay. Runtime wrapper is outside existing MFE,
        # therefore it only acts when the existing stack keeps the position open.
        if age > EARLY_MIN_AGE:
            if not adverse:
                adverse = base.adverse_three(direction, df3, now)
            if mfe < EARLY_MAX_MFE and current < 0.0 and adverse:
                close_leg(now, price, remaining, "early_failure_low_mfe_10m")
                break

        if now >= base.SQUARE_OFF:
            close_leg(now, price, remaining, "square_off")
            break

    if remaining > 0:
        last = rows.iloc[-1]
        close_leg(last["date"], float(last["close"]), remaining, "square_off_fallback")

    return {
        "legs": legs,
        "exit_time": max(x["time"] for x in legs),
        "net": sum(x["net"] for x in legs),
        "gross": sum(x["gross"] for x in legs),
        "costs": sum(x["costs"] for x in legs),
        "mfe": max_mfe,
        "mae": min_mae,
        "reasons": " + ".join(x["reason"] for x in legs),
    }


def replay_day(session_date, kite):
    harness.configure_date(session_date)
    opportunities = base.group_history()
    if not opportunities:
        return {"date": session_date, "error": "no source opportunities"}

    symbols = sorted({x["symbol"] for x in opportunities})
    print(f"\nFetching Kite history for {session_date} | symbols={len(symbols)}")
    minute, three = base.fetch_market_data(kite, symbols)

    enriched = []
    for op in opportunities:
        row = base.indicator_row(three.get(op["symbol"]), op["signal_start"])
        if row is None:
            op["direction"] = None
            op["direction_reason"] = "MISSING_INDICATOR"
        else:
            ema9 = float(row["ema9"])
            ema21 = float(row["ema21"])
            rsi = float(row["rsi14"]) if not pd.isna(row["rsi14"]) else None
            direction, base_direction, override = final_direction(op.get("adx"), ema9, ema21, rsi)
            op.update({
                "ema9": ema9, "ema21": ema21, "rsi": rsi,
                "direction": direction, "base_direction": base_direction,
                "rsi_override": override,
            })
        enriched.append(op)

    accepted = []
    rejected = []
    halt_time = None

    for op in enriched:
        now = op["order_time"]

        prior_legs = sorted(
            [leg for t in accepted for leg in t["replay"]["legs"] if leg["time"] <= now],
            key=lambda x: x["time"],
        )
        running = 0.0
        for leg in prior_legs:
            running += leg["net"]
            if running <= -DAILY_LOSS_LIMIT:
                halt_time = halt_time or leg["time"]
                break
        if halt_time is not None and now >= halt_time:
            rejected.append((op, "DAILY_LOSS_5PCT_HALT"))
            continue

        if ENABLE_ENTRY_CUTOFF and now.time() >= ENTRY_CUTOFF:
            rejected.append((op, "ENTRY_AT_OR_AFTER_14_00"))
            continue

        if op.get("adx") is not None and float(op["adx"]) < 20.0:
            rejected.append((op, "ADX_LT20_BLOCK"))
            continue

        direction = op.get("direction")
        if direction not in {"BUY", "SELL"}:
            rejected.append((op, "DIRECTION_UNAVAILABLE"))
            continue

        # Running bot cannot safely represent two simultaneous positions in
        # the same symbol because open_positions is keyed by symbol.
        open_same = [
            t for t in accepted
            if t["symbol"] == op["symbol"] and t["entry_time"] <= now < t["replay"]["exit_time"]
        ]
        if open_same:
            rejected.append((op, "SYMBOL_ALREADY_OPEN"))
            continue

        completed_symbol = sorted(
            [
                t for t in accepted
                if t["symbol"] == op["symbol"] and t["replay"]["exit_time"] <= now
            ],
            key=lambda t: t["replay"]["exit_time"],
        )
        trailing_losses = 0
        for t in reversed(completed_symbol):
            if t["replay"]["net"] < 0:
                trailing_losses += 1
            else:
                break
        if trailing_losses >= CONSECUTIVE_LOSS_BLOCK:
            rejected.append((op, "TWO_CONSECUTIVE_SYMBOL_LOSSES"))
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

        result = replay_trade(op, direction, qty, df1, df3)
        if result is None:
            rejected.append((op, "NO_EXIT_HISTORY"))
            continue

        accepted.append({**op, "qty": qty, "entry_time": now, "replay": result})

    net = sum(t["replay"]["net"] for t in accepted)
    gross = sum(t["replay"]["gross"] for t in accepted)
    costs = sum(t["replay"]["costs"] for t in accepted)
    wins = sum(t["replay"]["net"] > 0 for t in accepted)
    losses = sum(t["replay"]["net"] < 0 for t in accepted)
    actual = sum(float(o["actual_net"]) for o in opportunities)

    print("\n" + "=" * 145)
    print(f"{session_date} — LOSS-REDUCTION PAPER REPLAY")
    print("ADX<20 BLOCK | 20-<40 REVERSE | >=40 NORMAL | "
          + ("entries <14:00 | " if ENABLE_ENTRY_CUTOFF else "NO TIME CUTOFF | ")
          + "2 consecutive-loss symbol block | early low-MFE exit")
    print("=" * 145)
    print(f"SOURCE OPPORTUNITIES : {len(opportunities)}")
    print(f"TRADES USED          : {len(accepted)}")
    print(f"NOT TRADED           : {len(rejected)}")
    print(f"WINS / LOSSES        : {wins} / {losses}")
    print(f"WIN RATE             : {(wins/len(accepted)*100 if accepted else 0):.2f}%")
    print(f"GROSS P&L            : Rs {gross:.2f}")
    print(f"COSTS                : Rs {costs:.2f}")
    print(f"NET P&L              : Rs {net:.2f}")
    print(f"RETURN ON CAPITAL    : {(net/base.CAPITAL*100):.3f}%")
    print(f"ACTUAL SOURCE NET    : Rs {actual:.2f}")
    print(f"CHANGE VS ACTUAL     : Rs {net-actual:+.2f}")

    rejection_counts = Counter(reason for _, reason in rejected)
    print("\nNOT-TRADED REASONS")
    for reason, count in rejection_counts.most_common():
        print(f"  {reason:<38} {count}")

    exit_counts = Counter()
    exit_net = defaultdict(float)
    for t in accepted:
        for leg in t["replay"]["legs"]:
            exit_counts[leg["reason"]] += 1
            exit_net[leg["reason"]] += leg["net"]
    print("\nEXIT DISTRIBUTION")
    for reason, count in exit_counts.most_common():
        print(f"  {reason:<38} count={count:<3} net=Rs {exit_net[reason]:>9.2f}")

    print("\nTRADE DETAIL")
    for t in accepted:
        r = t["replay"]
        print(
            f"{t['order_time'].strftime('%H:%M:%S')} {t['symbol']:<13} ADX={t.get('adx')} "
            f"{t['direction']:<4} qty={t['qty']:<3} net={r['net']:>8.2f} MFE={r['mfe']:.3f}% MAE={r['mae']:.3f}% {r['reasons']}"
        )

    return {
        "date": session_date, "source": len(opportunities), "used": len(accepted),
        "wins": wins, "losses": losses, "gross": gross, "costs": costs,
        "net": net, "actual": actual, "return_pct": net/base.CAPITAL*100.0,
    }


def main():
    import argparse, sys as _sys
    global ENABLE_ENTRY_CUTOFF
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--no-entry-cutoff", action="store_true",
                     help="disable the 14:00 entry ban to isolate the targeted rules")
    _args, _ = _ap.parse_known_args()
    if _args.no_entry_cutoff:
        ENABLE_ENTRY_CUTOFF = False
        print("*** 14:00 ENTRY CUTOFF DISABLED for this run ***\n")

    print("Connecting to Kite...")
    kite = base.get_kite_client()
    results = [replay_day(d, kite) for d in DATES]

    total_gross = sum(r.get("gross", 0.0) for r in results)
    total_costs = sum(r.get("costs", 0.0) for r in results)
    total_net = sum(r.get("net", 0.0) for r in results)
    total_actual = sum(r.get("actual", 0.0) for r in results)
    total_used = sum(r.get("used", 0) for r in results)
    total_wins = sum(r.get("wins", 0) for r in results)
    total_losses = sum(r.get("losses", 0) for r in results)

    print("\n" + "=" * 145)
    print("TWO-DAY PROFIT STATEMENT — LOSS-REDUCTION PAPER POLICY")
    print("=" * 145)
    for r in results:
        if r.get("error"):
            print(f"{r['date']}: {r['error']}")
        else:
            print(
                f"{r['date']} | trades={r['used']} W/L={r['wins']}/{r['losses']} "
                f"gross=Rs {r['gross']:.2f} costs=Rs {r['costs']:.2f} net=Rs {r['net']:.2f} return={r['return_pct']:.3f}%"
            )
    print("-" * 145)
    print(f"TOTAL TRADES          : {total_used}")
    print(f"TOTAL WINS / LOSSES   : {total_wins} / {total_losses}")
    print(f"COMBINED WIN RATE     : {(total_wins/total_used*100 if total_used else 0):.2f}%")
    print(f"TWO-DAY GROSS P&L     : Rs {total_gross:.2f}")
    print(f"TWO-DAY COSTS         : Rs {total_costs:.2f}")
    print(f"TWO-DAY NET P&L       : Rs {total_net:.2f}")
    print(f"TWO-DAY RETURN        : {(total_net/base.CAPITAL*100):.3f}%")
    print(f"TWO-DAY ACTUAL NET    : Rs {total_actual:.2f}")
    print(f"IMPROVEMENT VS ACTUAL : Rs {total_net-total_actual:+.2f}")
    print("\nNOTE: same-executed-opportunity counterfactual; minute-close replay proxy, not tick-exact execution.")


if __name__ == "__main__":
    main()
