"""Read-only Aug-12 replay: wait up to two completed 3-minute bars for a candlestick pattern.

Research question
-----------------
The first candlestick-gate replay required a qualifying pattern on the exact
upstream signal candle. 71/75 trades therefore returned NO_PATTERN. This script
keeps the original Aug-12 trade direction frozen, but allows the isolated
candlestick engine to keep observing the next two *completed* 3-minute bars.

Important safety / interpretation
---------------------------------
- Historical Kite data reads only.
- No broker orders.
- No production state writes.
- Results are written only to /tmp.
- The actual trade P&L is grouped diagnostically; it is NOT simulated P&L for
  delayed entries because a delayed fill would change the subsequent exit path.
- Existing candlestick geometry, volume rules, VWAP/EMA50 context, 0.20% PAPER
  sizing and 2R planning remain unchanged.
- NEXT_OPEN confirmations are flagged and must not be treated as production-safe
  fills until a real bar-boundary execution path exists.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import pandas as pd

from auth import get_kite_client
from candlestick_engine import CandlestickEngine, EngineConfig, GateState, Trigger, evaluate_trade_entry
from replay_candlestick_gate_aug12 import (
    EQUITY,
    SESSION_DATE,
    OUTPUT_CSV as BASELINE_OUTPUT_CSV,
    build_instrument_maps,
    completed_slice,
    fetch_symbol_candles,
    finite_or_none,
    load_aug12_trades,
    next_completed_bars,
    signal_timestamp,
)

OUTPUT_CSV = "/tmp/candlestick_wait2_aug12.csv"
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)


def _plan_fields(plan):
    if plan is None:
        return {
            "confirmed_pattern": None,
            "trigger": None,
            "plan_entry": None,
            "plan_stop": None,
            "plan_target": None,
            "planned_risk": None,
            "rr": None,
            "next_open_semantics": False,
        }
    return {
        "confirmed_pattern": plan.pattern.value,
        "trigger": plan.trigger.value,
        "plan_entry": float(plan.entry_price),
        "plan_stop": float(plan.stop_price),
        "plan_target": float(plan.target_price),
        "planned_risk": float(plan.planned_risk),
        "rr": float(plan.rr),
        "next_open_semantics": plan.trigger == Trigger.NEXT_OPEN,
    }


def replay_trade_wait2(trade: dict, candles: pd.DataFrame, tick_size: float) -> dict:
    symbol = str(trade.get("symbol") or "")
    exchange = str(trade.get("exchange") or "NSE")
    direction = str(trade.get("direction") or "").upper().strip()
    sig_ts, ts_source = signal_timestamp(trade)

    row = {
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "actual_pnl": finite_or_none(trade.get("pnl")),
        "signal_timestamp": sig_ts.isoformat() if sig_ts is not None else None,
        "timestamp_source": ts_source,
        "signal_state": None,
        "signal_pattern": None,
        "bar1_state": None,
        "bar1_pattern": None,
        "bar2_state": None,
        "bar2_pattern": None,
        "final_outcome": None,
        "confirm_delay_bars": None,
        "confirmed_pattern": None,
        "trigger": None,
        "plan_entry": None,
        "plan_stop": None,
        "plan_target": None,
        "planned_risk": None,
        "rr": None,
        "next_open_semantics": False,
        "bars_at_signal": 0,
        "error": None,
    }

    if sig_ts is None:
        row["final_outcome"] = "UNRESOLVED_TIMESTAMP"
        return row
    if direction not in {"BUY", "SELL"}:
        row["final_outcome"] = "INVALID_DIRECTION"
        return row

    signal_hist = completed_slice(candles, sig_ts)
    row["bars_at_signal"] = len(signal_hist)
    warmup = max(CFG.volume_lookback, CFG.body_lookback) + 5
    if len(signal_hist) < warmup:
        row["final_outcome"] = "INSUFFICIENT_HISTORY"
        return row

    engine = CandlestickEngine(CFG)
    result = evaluate_trade_entry(symbol, signal_hist, direction, EQUITY, tick_size, engine)
    row["signal_state"] = result.state.value
    row["signal_pattern"] = result.pattern.value if result.pattern is not None else None
    if result.setup is not None and result.setup.trigger == Trigger.NEXT_OPEN:
        row["next_open_semantics"] = True

    if result.state == GateState.CONFIRMED:
        row["final_outcome"] = "CONFIRMED_SIGNAL"
        row["confirm_delay_bars"] = 0
        row.update(_plan_fields(result.plan))
        return row

    future_closes = next_completed_bars(candles, sig_ts, 2)
    last_state = result.state

    for delay, close_ts in enumerate(future_closes, start=1):
        step_hist = completed_slice(candles, close_ts)
        step = evaluate_trade_entry(symbol, step_hist, direction, EQUITY, tick_size, engine)

        row[f"bar{delay}_state"] = step.state.value
        row[f"bar{delay}_pattern"] = step.pattern.value if step.pattern is not None else None
        if step.setup is not None and step.setup.trigger == Trigger.NEXT_OPEN:
            row["next_open_semantics"] = True
        last_state = step.state

        if step.state == GateState.CONFIRMED:
            row["final_outcome"] = f"CONFIRMED_BAR_{delay}"
            row["confirm_delay_bars"] = delay
            row.update(_plan_fields(step.plan))
            return row

    if last_state == GateState.WAITING:
        row["final_outcome"] = "PENDING_UNCONFIRMED_AFTER_2"
    else:
        row["final_outcome"] = "NO_PATTERN_AFTER_2"
    return row


def _load_baseline_outcomes():
    try:
        baseline = pd.read_csv(BASELINE_OUTPUT_CSV)
    except Exception:
        return {}
    required = {"symbol", "direction", "signal_timestamp", "final_outcome"}
    if not required.issubset(baseline.columns):
        return {}
    out = defaultdict(list)
    for _, r in baseline.iterrows():
        key = (str(r["symbol"]), str(r["direction"]), str(r["signal_timestamp"]))
        out[key].append(str(r["final_outcome"]))
    return out


def print_summary(rows: list[dict]):
    outcomes = Counter(r["final_outcome"] for r in rows)
    print("\n===== WAIT-UP-TO-2-BARS CANDLESTICK REPLAY =====")
    print(f"Trades analysed: {len(rows)}")
    for key in sorted(outcomes):
        print(f"{key}: {outcomes[key]}")

    confirmed = [r for r in rows if str(r["final_outcome"]).startswith("CONFIRMED_")]
    print(f"\nConfirmed within signal+2-bar window: {len(confirmed)}/{len(rows)}")

    pnl_by_outcome = defaultdict(float)
    count_by_outcome = defaultdict(int)
    for r in rows:
        pnl = r.get("actual_pnl")
        if pnl is not None:
            pnl_by_outcome[r["final_outcome"]] += pnl
            count_by_outcome[r["final_outcome"]] += 1

    print("\n===== ORIGINAL ACTUAL P&L GROUPED BY WAIT2 OUTCOME =====")
    print("Diagnostic only; NOT simulated delayed-entry P&L.")
    for key in sorted(pnl_by_outcome):
        print(f"{key}: count={count_by_outcome[key]} actual_pnl_sum={pnl_by_outcome[key]:.2f}")

    if confirmed:
        actual_sum = sum((r.get("actual_pnl") or 0.0) for r in confirmed)
        wins = sum(1 for r in confirmed if (r.get("actual_pnl") or 0.0) > 0)
        losses = sum(1 for r in confirmed if (r.get("actual_pnl") or 0.0) < 0)
        print("\n===== CONFIRMED-ROW DIAGNOSTIC =====")
        print(f"confirmed={len(confirmed)} wins_in_original_trades={wins} losses_in_original_trades={losses}")
        print(f"original_actual_pnl_sum_for_confirmed_rows={actual_sum:.2f}")

    next_open = [r for r in rows if r.get("next_open_semantics")]
    print(f"\nNEXT_OPEN semantic-risk rows: {len(next_open)}")

    print("\n===== SAPPHIRE / POLYPLEX =====")
    focus = [r for r in rows if r["symbol"] in {"SAPPHIRE", "POLYPLEX"}]
    for r in focus:
        print(
            f"{r['symbol']} {r['direction']} signal={r['signal_timestamp']} "
            f"signal={r['signal_state']}/{r['signal_pattern']} "
            f"bar1={r['bar1_state']}/{r['bar1_pattern']} "
            f"bar2={r['bar2_state']}/{r['bar2_pattern']} "
            f"outcome={r['final_outcome']} pattern={r['confirmed_pattern']} "
            f"actual_pnl={r['actual_pnl']}"
        )

    baseline = _load_baseline_outcomes()
    if baseline:
        changed = 0
        for r in rows:
            key = (r["symbol"], r["direction"], r["signal_timestamp"])
            if key in baseline and baseline[key] and baseline[key][0] != r["final_outcome"]:
                changed += 1
        print(f"\nRows whose classification differs from the exact-signal baseline file: {changed}")


def main():
    print("READ-ONLY MODE: historical data only; no orders; no production-state writes.")
    print("Experiment: freeze original direction and wait up to two completed 3-minute bars for a qualifying pattern.")

    trades = load_aug12_trades()
    if not trades:
        raise SystemExit(f"No trades found for {SESSION_DATE}")
    print(f"Loaded {len(trades)} Aug-12 trade-history rows")

    kite = get_kite_client()
    exchanges = {str(t.get("exchange") or "NSE") for t in trades}
    token_map, tick_map = build_instrument_maps(kite, exchanges)

    cache = {}
    rows = []
    for idx, trade in enumerate(trades, start=1):
        symbol = str(trade.get("symbol") or "")
        exchange = str(trade.get("exchange") or "NSE")
        key = (exchange, symbol)
        token = token_map.get(key)

        if not token:
            row = replay_trade_wait2(trade, pd.DataFrame(), tick_map.get(key, 0.05))
            row["final_outcome"] = "INSTRUMENT_NOT_FOUND"
            row["error"] = f"No instrument token for {exchange}:{symbol}"
        else:
            if key not in cache:
                try:
                    cache[key] = fetch_symbol_candles(kite, token)
                except Exception as exc:
                    cache[key] = pd.DataFrame()
                    print(f"Historical fetch failed for {exchange}:{symbol}: {exc}")
            try:
                row = replay_trade_wait2(trade, cache[key], tick_map.get(key, 0.05))
            except Exception as exc:
                row = {
                    "symbol": symbol,
                    "exchange": exchange,
                    "direction": str(trade.get("direction") or ""),
                    "actual_pnl": finite_or_none(trade.get("pnl")),
                    "final_outcome": "ERROR",
                    "error": repr(exc),
                }

        rows.append(row)
        print(f"[{idx}/{len(trades)}] {exchange}:{symbol} {row.get('direction')} -> {row.get('final_outcome')}")

    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False)
    print_summary(rows)
    print(f"\nDetailed CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
