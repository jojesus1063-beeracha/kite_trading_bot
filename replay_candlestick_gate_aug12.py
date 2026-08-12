"""Read-only Aug-12 candlestick-gate counterfactual over actual PAPER trades.

This script does NOT replay the full strategy. It takes each actual Aug-12 trade's
already-decided direction as the upstream intended_direction, reconstructs the
completed 3-minute candles available at that trade's signal time, and asks the
isolated candlestick entry-timing gate whether the trade would be immediately
confirmed, delayed (up to two completed bars), or blocked.

Safety / interpretation:
- Kite usage is historical-data READ ONLY.
- No broker orders are submitted.
- No production state files are written.
- Output CSV is written only to /tmp.
- Actual trade P&L is reported only as a diagnostic grouping. It is NOT a
  counterfactual strategy P&L, because delayed/changed entries alter fills/exits.
- NEXT_OPEN plans are flagged separately because the current engine confirms them
  only after the next completed bar is supplied, while using that bar's open.
  That is useful for state-machine analysis but must not be treated as a
  production-safe replay fill without a real bar-boundary execution event.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

import pandas as pd

from auth import get_kite_client
from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Trigger,
    evaluate_trade_entry,
)
from trade_log import LOG_PATH

SESSION_DATE = "2026-08-12"
IST = "Asia/Kolkata"
INTERVAL = "3minute"
BAR_MINUTES = 3
FROM_TS = pd.Timestamp("2026-08-11 09:15", tz=IST)
TO_TS = pd.Timestamp("2026-08-12 15:30", tz=IST)
OUTPUT_CSV = "/tmp/candlestick_gate_aug12.csv"
EQUITY = 5000.0
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)


def load_aug12_trades(path: str = LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Trade history not found: {path}")
    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("date")) == SESSION_DATE:
                trades.append(row)
    return trades


def to_ist(value, date_hint: str = SESSION_DATE):
    if value is None or value == "":
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?", text):
        text = f"{date_hint} {text}"
    try:
        ts = pd.Timestamp(text)
    except Exception:
        return None
    if ts.tzinfo is None:
        return ts.tz_localize(IST)
    return ts.tz_convert(IST)


def signal_timestamp(trade: dict):
    # Prefer the durable analytics field representing the exact completed signal
    # candle close. Fall back to entry_time only if older records lack it.
    for key in ("signal_candle_close", "entry_time"):
        ts = to_ist(trade.get(key), str(trade.get("date") or SESSION_DATE))
        if ts is not None:
            return ts, key
    return None, None


def build_instrument_maps(kite, exchanges: set[str]):
    token_map = {}
    tick_map = {}
    for exchange in sorted(exchanges):
        rows = kite.instruments(exchange)
        for inst in rows:
            symbol = inst.get("tradingsymbol")
            if not symbol:
                continue
            key = (exchange, symbol)
            token_map[key] = inst.get("instrument_token")
            tick = inst.get("tick_size")
            if tick is not None:
                try:
                    tick = float(tick)
                except (TypeError, ValueError):
                    tick = None
            tick_map[key] = tick if tick and tick > 0 else 0.05
    return token_map, tick_map


def normalize_candles(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    tz = df["date"].dt.tz
    if tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    cols = ["date", "open", "high", "low", "close", "volume"]
    return df[cols].sort_values("date").reset_index(drop=True)


def fetch_symbol_candles(kite, token: int) -> pd.DataFrame:
    # Direct historical read. One request per unique symbol keeps the replay small
    # and avoids repeatedly fetching identical data for repeated trades.
    raw = kite.historical_data(
        token,
        FROM_TS.to_pydatetime(),
        TO_TS.to_pydatetime(),
        INTERVAL,
    )
    time.sleep(0.35)
    return normalize_candles(raw)


def completed_slice(df: pd.DataFrame, cutoff) -> pd.DataFrame:
    if df.empty:
        return df
    close_ts = df["date"] + pd.Timedelta(minutes=BAR_MINUTES)
    return df.loc[close_ts <= cutoff].copy().reset_index(drop=True)


def next_completed_bars(df: pd.DataFrame, cutoff, limit=2) -> list[pd.Timestamp]:
    close_ts = df["date"] + pd.Timedelta(minutes=BAR_MINUTES)
    return list(close_ts.loc[close_ts > cutoff].iloc[:limit])


def finite_or_none(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def replay_trade(trade: dict, candles: pd.DataFrame, tick_size: float) -> dict:
    symbol = str(trade.get("symbol") or "")
    exchange = str(trade.get("exchange") or "NSE")
    direction = str(trade.get("direction") or "").upper().strip()
    sig_ts, ts_source = signal_timestamp(trade)

    base = {
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "actual_pnl": finite_or_none(trade.get("pnl")),
        "actual_entry": finite_or_none(trade.get("entry")),
        "actual_exit": finite_or_none(trade.get("exit")),
        "signal_timestamp": sig_ts.isoformat() if sig_ts is not None else None,
        "timestamp_source": ts_source,
        "initial_state": None,
        "initial_pattern": None,
        "initial_trigger": None,
        "final_outcome": None,
        "confirm_delay_bars": None,
        "confirmed_pattern": None,
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
        base["final_outcome"] = "UNRESOLVED_TIMESTAMP"
        return base
    if direction not in {"BUY", "SELL"}:
        base["final_outcome"] = "INVALID_DIRECTION"
        return base

    hist = completed_slice(candles, sig_ts)
    base["bars_at_signal"] = len(hist)
    if len(hist) < max(CFG.volume_lookback, CFG.body_lookback) + 5:
        base["final_outcome"] = "INSUFFICIENT_HISTORY"
        return base

    engine = CandlestickEngine(CFG)
    result = evaluate_trade_entry(symbol, hist, direction, EQUITY, tick_size, engine)
    base["initial_state"] = result.state.value
    if result.pattern is not None:
        base["initial_pattern"] = result.pattern.value
    if result.setup is not None:
        base["initial_trigger"] = result.setup.trigger.value
        base["next_open_semantics"] = result.setup.trigger == Trigger.NEXT_OPEN

    if result.state == GateState.CONFIRMED:
        base["final_outcome"] = "CONFIRMED_IMMEDIATE"
        base["confirm_delay_bars"] = 0
        plan = result.plan
    elif result.state == GateState.NO_PATTERN:
        base["final_outcome"] = "NO_PATTERN"
        plan = None
    else:
        plan = None
        for delay, next_close in enumerate(next_completed_bars(candles, sig_ts, 2), start=1):
            step_hist = completed_slice(candles, next_close)
            step = evaluate_trade_entry(symbol, step_hist, direction, EQUITY, tick_size, engine)
            if step.state == GateState.CONFIRMED:
                base["final_outcome"] = f"CONFIRMED_DELAYED_{delay}"
                base["confirm_delay_bars"] = delay
                plan = step.plan
                if step.plan is not None and step.plan.trigger == Trigger.NEXT_OPEN:
                    base["next_open_semantics"] = True
                break
        if plan is None:
            base["final_outcome"] = "BLOCKED_AFTER_2_BARS"

    if plan is not None:
        base["confirmed_pattern"] = plan.pattern.value
        base["plan_entry"] = plan.entry_price
        base["plan_stop"] = plan.stop_price
        base["plan_target"] = plan.target_price
        base["planned_risk"] = plan.planned_risk
        base["rr"] = plan.rr
        if plan.trigger == Trigger.NEXT_OPEN:
            base["next_open_semantics"] = True

    return base


def print_summary(rows: list[dict]):
    outcomes = Counter(r["final_outcome"] for r in rows)
    print("\n===== CANDLESTICK GATE AUG-12 COUNTERFACTUAL =====")
    print(f"Trades analysed: {len(rows)}")
    for key in sorted(outcomes):
        print(f"{key}: {outcomes[key]}")

    pnl_by_outcome = defaultdict(float)
    count_by_outcome = defaultdict(int)
    for r in rows:
        pnl = r.get("actual_pnl")
        if pnl is not None:
            pnl_by_outcome[r["final_outcome"]] += pnl
            count_by_outcome[r["final_outcome"]] += 1

    print("\n===== ORIGINAL ACTUAL P&L GROUPED BY GATE OUTCOME =====")
    print("Diagnostic only; NOT simulated candlestick-strategy P&L.")
    for key in sorted(pnl_by_outcome):
        print(f"{key}: count={count_by_outcome[key]} actual_pnl_sum={pnl_by_outcome[key]:.2f}")

    next_open = [r for r in rows if r.get("next_open_semantics")]
    print(f"\nNEXT_OPEN semantic-risk rows: {len(next_open)}")
    print("These must not be treated as production-safe next-open fills until a real bar-boundary execution path exists.")

    focus = [r for r in rows if r["symbol"] in {"SAPPHIRE", "POLYPLEX"}]
    print("\n===== SAPPHIRE / POLYPLEX =====")
    if not focus:
        print("No matching Aug-12 trade-history rows found.")
    else:
        for r in focus:
            print(
                f"{r['symbol']} {r['direction']} signal={r['signal_timestamp']} "
                f"initial={r['initial_state']}/{r['initial_pattern']} "
                f"outcome={r['final_outcome']} delay={r['confirm_delay_bars']} "
                f"pattern={r['confirmed_pattern']} actual_pnl={r['actual_pnl']}"
            )


def main():
    print("READ-ONLY MODE: historical data only; no orders; no production-state writes.")
    trades = load_aug12_trades()
    if not trades:
        raise SystemExit(f"No trades found for {SESSION_DATE} in {LOG_PATH}")
    print(f"Loaded {len(trades)} Aug-12 trade-history rows from {LOG_PATH}")

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
            row = replay_trade(trade, pd.DataFrame(), tick_map.get(key, 0.05))
            row["final_outcome"] = "INSTRUMENT_NOT_FOUND"
            row["error"] = f"No instrument token for {exchange}:{symbol}"
            rows.append(row)
            print(f"[{idx}/{len(trades)}] {exchange}:{symbol} -> INSTRUMENT_NOT_FOUND")
            continue
        if key not in cache:
            try:
                cache[key] = fetch_symbol_candles(kite, token)
            except Exception as exc:
                cache[key] = pd.DataFrame()
                print(f"Historical fetch failed for {exchange}:{symbol}: {exc}")
        try:
            row = replay_trade(trade, cache[key], tick_map.get(key, 0.05))
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
        print(
            f"[{idx}/{len(trades)}] {exchange}:{symbol} {row.get('direction')} -> "
            f"{row.get('final_outcome')}"
        )

    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False)
    print_summary(rows)
    print(f"\nDetailed CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
