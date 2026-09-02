#!/usr/bin/env python3
"""A/B replay of the proposed post-entry failed-development exits.

ANALYSIS ONLY. This script never imports/runs main.py and cannot place orders.

Experiment design
-----------------
Current PAPER policy is retained:
- capital from config (expected Rs 5,000)
- risk/trade 0.20%
- ADX <20 BLOCK
- 20<=ADX<40 REVERSED EMA9/EMA21
- ADX>=40 NORMAL EMA9/EMA21
- RSI extreme override after the ADX gate
- no 14:00 cutoff
- no completed-count/cooldown frequency guards
- same-symbol concurrent entries blocked
- sticky 5% realized daily-loss halt
- aggregate realized-loss + open-risk + proposed-risk guard
- 0.45% sizing/strategy-stop geometry
- 0.75% PAPER emergency stop
- existing hybrid / MAE / MFE / dead-trade / square-off exits

Only the following post-entry rule is changed:

BASELINE
    No new failed-development exit.

VARIANT_A
    From +10 minutes after order submission onward:
      MFE-from-entry < +0.15% AND current P/L < 0
    -> close the remaining position.

VARIANT_B
    From +8 minutes onward:
      MAE-from-entry <= -0.15%
    -> close the remaining position.
    Otherwise VARIANT_A remains active from +10 minutes onward.

Selection protocol
------------------
1. Replay BASELINE, A and B on 11-Aug-2026.
2. Select the better of A/B by Aug-11 after-cost net P&L.
   Tie-break: smaller absolute max realized drawdown, then Variant A.
3. Run only that selected variant on 10-Aug-2026 as an out-of-sample check,
   while also printing the Aug-10 baseline for comparison.

Data-integrity policy
---------------------
The replay fails closed if Kite does not provide usable 1-minute and 3-minute
history for every symbol that could be traded after the non-history gates. Empty
or short 1-minute responses are retried with backoff. A partial-history replay
must never be mistaken for a valid strategy result.

Important limitations
---------------------
This is a same-source-opportunity counterfactual. It reuses opportunities found
in trade_history.jsonl; it does not recreate candidates that the historical bot
never generated. Kite historical 1-minute closes proxy the live 25-second
position monitor. The entry-path MFE/MAE intentionally uses the same
order-minute convention as the research that discovered the rule so the A/B
experiment tests that hypothesis without silently changing its definition.
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import replay_current_policy_20260811 as current

base = current.base
IST = base.IST
DATES = ("2026-08-11", "2026-08-10")
BASELINE = "BASELINE"
VARIANT_A = "VARIANT_A"
VARIANT_B = "VARIANT_B"
RISK_PER_TRADE_PCT = 0.20
RISK_AMOUNT = base.CAPITAL * RISK_PER_TRADE_PCT / 100.0
DAILY_LOSS_LIMIT = base.CAPITAL * 5.0 / 100.0
STRATEGY_STOP_PCT = base.STRATEGY_STOP_PCT
A_MINUTES = 10.0
A_MAX_MFE_PCT = 0.15
B_MINUTES = 8.0
B_MAE_PCT = -0.15
OUT_SUMMARY = Path("/tmp/failed_development_variant_summary.csv")
OUT_TRADES = Path("/tmp/failed_development_variant_trades.csv")

# A normal NSE cash session has roughly 355 one-minute bars. We only require
# enough coverage to establish that the response is real rather than an empty /
# transient API result. Exact session length is not hard-coded as a pass rule.
MIN_USABLE_MINUTE_ROWS = 300
MIN_USABLE_3MIN_ROWS = 100
HISTORY_RETRIES = 4
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 7.0)


@dataclass
class ReplayContext:
    session_date: str
    square_off: pd.Timestamp


def configure_date(session_date: str) -> ReplayContext:
    base.SESSION_DATE = session_date
    square_off = pd.Timestamp(
        f"{session_date} {getattr(base.cfg, 'FORCE_SQUARE_OFF_TIME', '15:08')}",
        tz=IST,
    )
    base.SQUARE_OFF = square_off
    return ReplayContext(session_date=session_date, square_off=square_off)


def failed_development_reason(
    variant: str,
    entry_age_minutes: float,
    mfe_from_entry_pct: float,
    mae_from_entry_pct: float,
    current_pct: float,
):
    """Pure rule helper used by replay and unit tests."""
    if variant == BASELINE:
        return None

    if variant == VARIANT_B and entry_age_minutes >= B_MINUTES:
        if mae_from_entry_pct <= B_MAE_PCT:
            return "failed_development_B_8m_mae_m0_15"

    if variant in {VARIANT_A, VARIANT_B} and entry_age_minutes >= A_MINUTES:
        if mfe_from_entry_pct < A_MAX_MFE_PCT and current_pct < 0.0:
            return "failed_development_A_10m_mfe_lt_0_15_current_neg"

    return None


def _fetch_with_retry(kite, token, from_dt, to_dt, interval, min_rows, label):
    """Fetch history with bounded retries; return a prepared DataFrame."""
    last_df = pd.DataFrame()
    last_error = None
    for attempt in range(1, HISTORY_RETRIES + 1):
        try:
            rows = kite.historical_data(token, from_dt, to_dt, interval)
            last_df = base.prepare_df(rows)
            if len(last_df) >= min_rows:
                return last_df
            last_error = f"only {len(last_df)} rows"
        except Exception as exc:  # analysis-only fetch; retry transient broker/API errors
            last_error = f"{type(exc).__name__}: {exc}"
            last_df = pd.DataFrame()

        if attempt < HISTORY_RETRIES:
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            print(
                f"  RETRY {label} attempt={attempt}/{HISTORY_RETRIES} "
                f"reason={last_error} wait={delay:.1f}s"
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Historical data incomplete for {label} after {HISTORY_RETRIES} attempts: {last_error}"
    )


def fetch_market_data_reliable(kite, symbols, session_date):
    """Fetch complete 1m + 3m data and fail closed on partial history."""
    instruments = {
        x["tradingsymbol"]: x["instrument_token"]
        for x in kite.instruments("NSE")
        if x.get("instrument_type") == "EQ" and x.get("tradingsymbol") in symbols
    }
    missing_tokens = sorted(set(symbols) - set(instruments))
    if missing_tokens:
        raise RuntimeError(
            "Missing NSE EQ instrument tokens for: " + ", ".join(missing_tokens)
        )

    minute = {}
    three = {}
    session_from = pd.Timestamp(f"{session_date} 09:15", tz=IST).to_pydatetime()
    session_to = pd.Timestamp(f"{session_date} 15:09", tz=IST).to_pydatetime()
    warmup_from = pd.Timestamp(
        pd.Timestamp(session_date) - pd.Timedelta(days=6), tz=IST
    ).replace(hour=9, minute=15).to_pydatetime()

    print(f"Fetching Kite history with integrity checks for {len(instruments)} symbols...")
    for idx, symbol in enumerate(sorted(instruments), start=1):
        token = instruments[symbol]
        one = _fetch_with_retry(
            kite,
            token,
            session_from,
            session_to,
            "minute",
            MIN_USABLE_MINUTE_ROWS,
            f"{session_date} {symbol} 1minute",
        )
        # Keep request rate conservative even when the first attempt succeeds.
        time.sleep(0.45)
        tri = _fetch_with_retry(
            kite,
            token,
            warmup_from,
            session_to,
            "3minute",
            MIN_USABLE_3MIN_ROWS,
            f"{session_date} {symbol} 3minute",
        )
        time.sleep(0.45)
        minute[symbol] = one
        three[symbol] = base.add_rsi_ema(tri)
        print(
            f"[{idx:02d}/{len(instruments):02d}] {symbol:<14} "
            f"minute={len(minute[symbol]):3d} 3minute={len(three[symbol]):4d} OK"
        )

    return minute, three


def enrich_current_direction(opportunities, three):
    enriched = []
    for source in opportunities:
        op = dict(source)
        df3 = three.get(op["symbol"])
        row = base.indicator_row(df3, op["signal_start"])
        if row is None:
            op["indicator_error"] = "missing 3-minute indicator row"
            enriched.append(op)
            continue

        ema9 = float(row["ema9"])
        ema21 = float(row["ema21"])
        rsi = float(row["rsi14"]) if not pd.isna(row["rsi14"]) else None
        final, base_direction, override = current.current_direction(
            op.get("adx"), ema9, ema21, rsi
        )
        op.update(
            {
                "ema9": ema9,
                "ema21": ema21,
                "rsi": rsi,
                "proposed_direction": final,
                "proposed_base": base_direction,
                "rsi_override": override,
            }
        )
        enriched.append(op)
    return enriched


def replay_trade_variant(op, direction, qty, df1, df3, variant, square_off):
    """Mirror the existing replay stack and insert exactly one A/B overlay."""
    if variant == BASELINE:
        return base.replay_trade(op, direction, qty, df1, df3)

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
    variant_trigger = None
    variant_trigger_age = None
    variant_trigger_mfe = None
    variant_trigger_mae = None
    variant_trigger_current = None

    def close_leg(now, price, amount, reason):
        nonlocal remaining
        amount = min(int(amount), remaining)
        if amount <= 0:
            return
        c = base.cost_leg(direction, amount, entry, price)
        legs.append(
            {
                "time": now,
                "qty": amount,
                "price": float(price),
                "reason": reason,
                **c,
            }
        )
        remaining -= amount

    for _, row in rows.iterrows():
        now = row["date"]
        price = float(row["close"])

        since_strategy = df1.loc[
            (df1["date"] >= timer_start.floor("min")) & (df1["date"] <= now)
        ]
        mfe_strategy, mae_strategy, current_pct, giveback = base.excursions(
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

        hit_emergency = (
            price <= emergency_stop if direction == "BUY" else price >= emergency_stop
        )
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
                close_leg(
                    now,
                    price,
                    remaining,
                    "hybrid_runner_2r" if hybrid else "fixed_target",
                )
                break

        reason = failed_development_reason(
            variant,
            entry_age,
            mfe_entry,
            mae_entry,
            current_entry,
        )
        if reason:
            variant_trigger = reason
            variant_trigger_age = entry_age
            variant_trigger_mfe = mfe_entry
            variant_trigger_mae = mae_entry
            variant_trigger_current = current_entry
            close_leg(now, price, remaining, reason)
            break

        if strategy_age > base.MAE_MIN_AGE:
            adverse = base.adverse_three(direction, df3, now)
            if (
                mae_strategy <= base.MAE_THRESHOLD
                and current_pct <= base.CURRENT_LOSS_THRESHOLD
                and mfe_strategy < base.MAX_MFE_FAILURE
                and adverse
            ):
                close_leg(now, price, remaining, "mae_adverse_trend_10m")
                break

        reason = base.mfe_reason(strategy_age, mfe_strategy, current_pct, giveback)
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
        "variant_trigger": variant_trigger,
        "variant_trigger_age": variant_trigger_age,
        "variant_trigger_mfe": variant_trigger_mfe,
        "variant_trigger_mae": variant_trigger_mae,
        "variant_trigger_current": variant_trigger_current,
    }


def completed_legs(accepted, now):
    return sorted(
        [
            leg
            for trade in accepted
            for leg in trade["replay"]["legs"]
            if leg["time"] <= now
        ],
        key=lambda x: x["time"],
    )


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


def replay_policy(session_date, variant, opportunities, minute, three, square_off):
    enriched = enrich_current_direction(opportunities, three)
    accepted = []
    rejected = []
    sticky_halt_time = None

    for op in enriched:
        now = op["order_time"]
        prior_legs = completed_legs(accepted, now)
        realized = sum(float(leg["net"]) for leg in prior_legs)

        if sticky_halt_time is None:
            running = 0.0
            for leg in prior_legs:
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

        realized_loss = max(0.0, -realized)
        open_risk = sum(
            strategy_risk_for(t["entry"], remaining_qty_at(t, now)) for t in open_now
        )
        proposed_risk = strategy_risk_for(op["entry"], qty)
        aggregate = realized_loss + open_risk + proposed_risk
        if aggregate >= DAILY_LOSS_LIMIT:
            rejected.append((op, "AGGREGATE_DAILY_RISK_GTE_BUDGET"))
            continue

        df1 = minute.get(op["symbol"])
        df3 = three.get(op["symbol"])
        if df1 is None or df1.empty or df3 is None or df3.empty:
            # This should be impossible after the integrity-checked loader.
            raise RuntimeError(
                f"DATA INTEGRITY FAILURE inside replay for {session_date} {op['symbol']}"
            )

        result = replay_trade_variant(
            op, direction, qty, df1, df3, variant, square_off
        )
        if result is None:
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
                "replay": result,
                "admission": {
                    "realized_pnl": realized,
                    "realized_loss": realized_loss,
                    "open_risk": open_risk,
                    "proposed_risk": proposed_risk,
                    "aggregate": aggregate,
                },
            }
        )

    all_legs = sorted(
        [leg for trade in accepted for leg in trade["replay"]["legs"]],
        key=lambda x: x["time"],
    )
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    eod_halt = sticky_halt_time
    for leg in all_legs:
        running += float(leg["net"])
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)
        if eod_halt is None and running <= -DAILY_LOSS_LIMIT:
            eod_halt = leg["time"]

    gross = sum(float(t["replay"]["gross"]) for t in accepted)
    costs = sum(float(t["replay"]["costs"]) for t in accepted)
    net = sum(float(t["replay"]["net"]) for t in accepted)
    wins = sum(1 for t in accepted if t["replay"]["net"] > 0)
    losses = sum(1 for t in accepted if t["replay"]["net"] < 0)
    flats = len(accepted) - wins - losses
    variant_exits = sum(bool(t["replay"].get("variant_trigger")) for t in accepted)
    rejection_counts = Counter(reason for _, reason in rejected)
    exit_counts = Counter()
    exit_net = defaultdict(float)
    for trade in accepted:
        for leg in trade["replay"]["legs"]:
            exit_counts[leg["reason"]] += 1
            exit_net[leg["reason"]] += float(leg["net"])

    return {
        "date": session_date,
        "variant": variant,
        "source": len(opportunities),
        "accepted": accepted,
        "rejected": rejected,
        "trade_count": len(accepted),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": wins / len(accepted) * 100.0 if accepted else 0.0,
        "gross": gross,
        "costs": costs,
        "net": net,
        "return_pct": net / base.CAPITAL * 100.0,
        "max_drawdown": max_drawdown,
        "halt_time": eod_halt,
        "variant_exits": variant_exits,
        "rejection_counts": rejection_counts,
        "exit_counts": exit_counts,
        "exit_net": exit_net,
    }


def print_summary(result, baseline_net=None):
    print("\n" + "=" * 114)
    print(f"{result['date']} | {result['variant']}")
    print("=" * 114)
    print(f"SOURCE OPPORTUNITIES : {result['source']}")
    print(f"TRADES TAKEN         : {result['trade_count']}")
    print(
        f"WINS/LOSSES/FLAT     : {result['wins']} / {result['losses']} / {result['flats']} "
        f"| WR {result['win_rate']:.2f}%"
    )
    print(f"GROSS                : Rs {result['gross']:.2f}")
    print(f"COSTS                : Rs {result['costs']:.2f}")
    print(f"NET                  : Rs {result['net']:.2f}")
    print(f"RETURN               : {result['return_pct']:.3f}%")
    print(f"MAX REALIZED DD      : Rs {result['max_drawdown']:.2f}")
    print(f"VARIANT EXITS        : {result['variant_exits']}")
    print(
        "DAILY HALT           : "
        + (f"YES at {result['halt_time']}" if result["halt_time"] is not None else "NO")
    )
    if baseline_net is not None:
        print(f"CHANGE VS BASELINE   : Rs {result['net'] - baseline_net:+.2f}")

    if result["rejection_counts"]:
        print("REJECTIONS:")
        for reason, count in result["rejection_counts"].most_common():
            print(f"  {reason:<40} {count}")

    print("EXIT NET:")
    for reason, count in result["exit_counts"].most_common():
        print(
            f"  {reason:<48} count={count:<3} net=Rs {result['exit_net'][reason]:>9.2f}"
        )


def print_variant_trade_changes(result):
    changed = [t for t in result["accepted"] if t["replay"].get("variant_trigger")]
    if not changed:
        print("\nNo failed-development exits triggered.")
        return
    print("\nFAILED-DEVELOPMENT EXIT DETAIL")
    print(
        f"{'ENTRY':<9} {'SYMBOL':<13} {'DIR':>5} {'QTY':>4} {'NET':>9} "
        f"{'AGE':>6} {'MFE%':>7} {'MAE%':>7} {'CUR%':>7} REASON"
    )
    for trade in changed:
        r = trade["replay"]
        print(
            f"{trade['entry_time'].strftime('%H:%M:%S'):<9} {trade['symbol']:<13} "
            f"{trade['direction']:>5} {trade['qty']:>4} {r['net']:>9.2f} "
            f"{float(r['variant_trigger_age']):>6.1f} "
            f"{float(r['variant_trigger_mfe']):>7.3f} "
            f"{float(r['variant_trigger_mae']):>7.3f} "
            f"{float(r['variant_trigger_current']):>7.3f} {r['variant_trigger']}"
        )


def result_row(result, selected=False, phase=""):
    return {
        "phase": phase,
        "selected_variant": selected,
        "date": result["date"],
        "variant": result["variant"],
        "source_opportunities": result["source"],
        "trades": result["trade_count"],
        "wins": result["wins"],
        "losses": result["losses"],
        "flats": result["flats"],
        "win_rate_pct": result["win_rate"],
        "gross": result["gross"],
        "costs": result["costs"],
        "net": result["net"],
        "return_pct": result["return_pct"],
        "max_realized_drawdown": result["max_drawdown"],
        "variant_exits": result["variant_exits"],
        "daily_halt": result["halt_time"] is not None,
        "halt_time": result["halt_time"],
    }


def trade_rows(result, selected=False, phase=""):
    rows = []
    for trade in result["accepted"]:
        r = trade["replay"]
        rows.append(
            {
                "phase": phase,
                "selected_variant": selected,
                "date": result["date"],
                "variant": result["variant"],
                "entry_time": trade["entry_time"],
                "symbol": trade["symbol"],
                "direction": trade["direction"],
                "adx": trade.get("adx"),
                "qty": trade["qty"],
                "net": r["net"],
                "gross": r["gross"],
                "costs": r["costs"],
                "mfe": r["mfe"],
                "mae": r["mae"],
                "exit_time": r["exit_time"],
                "exit_reasons": r["reasons"],
                "variant_trigger": r.get("variant_trigger"),
                "variant_trigger_age": r.get("variant_trigger_age"),
                "variant_trigger_mfe": r.get("variant_trigger_mfe"),
                "variant_trigger_mae": r.get("variant_trigger_mae"),
                "variant_trigger_current": r.get("variant_trigger_current"),
            }
        )
    return rows


def choose_aug11_variant(a, b):
    if a["net"] > b["net"]:
        return a
    if b["net"] > a["net"]:
        return b
    if abs(a["max_drawdown"]) < abs(b["max_drawdown"]):
        return a
    if abs(b["max_drawdown"]) < abs(a["max_drawdown"]):
        return b
    return a


def load_day(kite, session_date):
    ctx = configure_date(session_date)
    opportunities = base.group_history()
    if not opportunities:
        raise SystemExit(f"No source opportunities found for {session_date}")
    symbols = sorted({op["symbol"] for op in opportunities})
    print(
        f"\nFetching {session_date} | source opportunities={len(opportunities)} "
        f"| symbols={len(symbols)}"
    )
    minute, three = fetch_market_data_reliable(kite, symbols, session_date)
    return ctx, opportunities, minute, three


def main():
    print("Connecting to Kite for ANALYSIS-ONLY A/B replay...")
    kite = base.get_kite_client()

    try:
        ctx11, opp11, minute11, three11 = load_day(kite, "2026-08-11")
    except Exception as exc:
        raise SystemExit(
            "DATA INTEGRITY BLOCK: Aug-11 replay aborted instead of using partial history. "
            f"{exc}"
        ) from exc

    aug11 = {}
    for variant in (BASELINE, VARIANT_A, VARIANT_B):
        aug11[variant] = replay_policy(
            ctx11.session_date,
            variant,
            opp11,
            minute11,
            three11,
            ctx11.square_off,
        )

    baseline11 = aug11[BASELINE]
    # Cross-check against the known complete-current-policy shape. The exact net
    # can vary by a few paise with refreshed historical data, but a collapse to
    # only a handful of trades is never acceptable.
    if baseline11["trade_count"] < 15:
        raise SystemExit(
            "DATA/REPLAY INTEGRITY BLOCK: Aug-11 baseline reconstructed only "
            f"{baseline11['trade_count']} trades; expected the complete replay universe "
            "to be around 20. No variant selection performed."
        )

    for variant in (BASELINE, VARIANT_A, VARIANT_B):
        print_summary(
            aug11[variant],
            baseline_net=None if variant == BASELINE else baseline11["net"],
        )
        if variant != BASELINE:
            print_variant_trade_changes(aug11[variant])

    selected11 = choose_aug11_variant(aug11[VARIANT_A], aug11[VARIANT_B])
    selected_name = selected11["variant"]
    print("\n" + "#" * 114)
    print(f"AUG-11 SELECTED VARIANT: {selected_name}")
    print(f"Selected net            : Rs {selected11['net']:.2f}")
    print(f"Aug-11 baseline net     : Rs {baseline11['net']:.2f}")
    print(f"Improvement vs baseline : Rs {selected11['net'] - baseline11['net']:+.2f}")
    print("#" * 114)

    try:
        ctx10, opp10, minute10, three10 = load_day(kite, "2026-08-10")
    except Exception as exc:
        raise SystemExit(
            "DATA INTEGRITY BLOCK: Aug-10 out-of-sample replay aborted instead of "
            f"using partial history. {exc}"
        ) from exc

    baseline10 = replay_policy(
        ctx10.session_date,
        BASELINE,
        opp10,
        minute10,
        three10,
        ctx10.square_off,
    )
    selected10 = replay_policy(
        ctx10.session_date,
        selected_name,
        opp10,
        minute10,
        three10,
        ctx10.square_off,
    )

    print("\n" + "#" * 114)
    print("OUT-OF-SAMPLE AUG-10 CHECK")
    print("#" * 114)
    print_summary(baseline10)
    print_summary(selected10, baseline_net=baseline10["net"])
    print_variant_trade_changes(selected10)

    combined_baseline = baseline11["net"] + baseline10["net"]
    combined_selected = selected11["net"] + selected10["net"]
    print("\n" + "=" * 114)
    print("FINAL TWO-DAY DECISION TABLE")
    print("=" * 114)
    print(f"Aug-11 baseline              : Rs {baseline11['net']:>9.2f}")
    print(f"Aug-11 {selected_name:<20}: Rs {selected11['net']:>9.2f}")
    print(f"Aug-10 baseline              : Rs {baseline10['net']:>9.2f}")
    print(f"Aug-10 {selected_name:<20}: Rs {selected10['net']:>9.2f}")
    print(f"Two-day baseline             : Rs {combined_baseline:>9.2f}")
    print(f"Two-day selected             : Rs {combined_selected:>9.2f}")
    print(f"Two-day improvement          : Rs {combined_selected - combined_baseline:>+9.2f}")
    print(f"Two-day selected return      : {combined_selected / base.CAPITAL * 100.0:>9.3f}%")
    profitable_both = selected11["net"] > 0 and selected10["net"] > 0
    beats_baseline_both = (
        selected11["net"] > baseline11["net"] and selected10["net"] > baseline10["net"]
    )
    print(f"Positive on BOTH days        : {'YES' if profitable_both else 'NO'}")
    print(f"Beats baseline on BOTH days  : {'YES' if beats_baseline_both else 'NO'}")
    print("DEPLOYMENT DECISION          : ANALYSIS ONLY — NO BOT CHANGE PERFORMED")

    summary_rows = [
        result_row(baseline11, phase="AUG11_SELECTION"),
        result_row(aug11[VARIANT_A], selected_name == VARIANT_A, "AUG11_SELECTION"),
        result_row(aug11[VARIANT_B], selected_name == VARIANT_B, "AUG11_SELECTION"),
        result_row(baseline10, phase="AUG10_OOS"),
        result_row(selected10, True, "AUG10_OOS"),
    ]
    pd.DataFrame(summary_rows).to_csv(OUT_SUMMARY, index=False)

    detail_rows = []
    for r in (baseline11, aug11[VARIANT_A], aug11[VARIANT_B], baseline10, selected10):
        selected = (
            (r["date"] == "2026-08-11" and r["variant"] == selected_name)
            or (r["date"] == "2026-08-10" and r["variant"] == selected_name)
        )
        phase = "AUG11_SELECTION" if r["date"] == "2026-08-11" else "AUG10_OOS"
        detail_rows.extend(trade_rows(r, selected, phase))
    pd.DataFrame(detail_rows).to_csv(OUT_TRADES, index=False)

    print("\nFILES")
    print(f"  {OUT_SUMMARY}")
    print(f"  {OUT_TRADES}")


if __name__ == "__main__":
    main()
