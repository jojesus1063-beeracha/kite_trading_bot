"""Read-only Aug-12 native-10m candlestick replay with true NEXT_OPEN semantics.

Purpose
-------
The earlier 10m research pass was built by resampling 3m candles.  Because 3m
and 10m boundaries are incommensurate, a derived 10m candle may not be fully
known until after the nominal next 10m bar has already opened.  Therefore the
old NEXT_OPEN confirmation could not be treated as an executable fill.

This script removes that ambiguity by fetching Kite's native ``10minute``
historical candles and handling NEXT_OPEN as an event at the actual next 10m
bar boundary:

1. Pattern is detected only from fully completed native 10m bars.
2. Pattern-forming volume is already enforced by candlestick_engine.py.
3. BUY/SELL context is checked using ONLY the completed setup bar:
      BUY  -> setup close > setup VWAP AND setup close > setup EMA50
      SELL -> setup close < setup VWAP AND setup close < setup EMA50
4. For Trigger.NEXT_OPEN, the trade plan is built from the next native bar's
   OPEN before any high/low/close/volume from that bar is consulted.
5. Stop/target simulation may then use that bar's subsequent OHLC because the
   position is already open from its first tick.

The candlestick engine itself is not modified.  This is a research-only replay:
no orders and no production-state writes.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter

import pandas as pd

from auth import get_kite_client
from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Side,
    Trigger,
    add_indicators,
    build_trade_plan,
    context_ok,
    evaluate_trade_entry,
)
from costs import net_pnl_for_trade
from trade_log import LOG_PATH

SESSION_DATE = "2026-08-12"
IST = "Asia/Kolkata"
INTERVAL = "10minute"
BAR_MINUTES = 10
FROM_TS = pd.Timestamp("2026-08-11 09:15", tz=IST)
TO_TS = pd.Timestamp("2026-08-12 15:30", tz=IST)
OUTPUT_CSV = "/tmp/native_10m_next_open_aug12.csv"
EQUITY = 5000.0
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)
WARMUP_BARS = max(CFG.volume_lookback, CFG.body_lookback) + 5
FOCUS = {"RHIM", "SENCO", "FORTIS", "SAPPHIRE", "POLYPLEX"}


def load_aug12_trades(path: str = LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Trade history not found: {path}")
    rows = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("date")) == SESSION_DATE:
                rows.append(row)
    return rows


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
    for key in ("signal_candle_close", "entry_time"):
        ts = to_ist(trade.get(key), str(trade.get("date") or SESSION_DATE))
        if ts is not None:
            return ts, key
    return None, None


def finite_or_none(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def normalize_candles(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    cols = ["date", "open", "high", "low", "close", "volume"]
    return df[cols].sort_values("date").reset_index(drop=True)


def build_instrument_maps(kite, exchanges: set[str]):
    token_map = {}
    tick_map = {}
    for exchange in sorted(exchanges):
        for inst in kite.instruments(exchange):
            symbol = inst.get("tradingsymbol")
            if not symbol:
                continue
            key = (exchange, symbol)
            token_map[key] = inst.get("instrument_token")
            try:
                tick = float(inst.get("tick_size"))
            except (TypeError, ValueError):
                tick = None
            tick_map[key] = tick if tick and tick > 0 else 0.05
    return token_map, tick_map


def fetch_native_10m(kite, token: int) -> pd.DataFrame:
    raw = kite.historical_data(
        token,
        FROM_TS.to_pydatetime(),
        TO_TS.to_pydatetime(),
        INTERVAL,
    )
    time.sleep(0.35)
    return normalize_candles(raw)


def completed_slice(candles: pd.DataFrame, cutoff) -> pd.DataFrame:
    if candles is None or candles.empty:
        return pd.DataFrame()
    close_times = candles["date"] + pd.Timedelta(minutes=BAR_MINUTES)
    return candles.loc[close_times <= cutoff].copy().reset_index(drop=True)


def next_completed_times(candles: pd.DataFrame, cutoff, limit: int) -> list[pd.Timestamp]:
    if candles is None or candles.empty:
        return []
    close_times = candles["date"] + pd.Timedelta(minutes=BAR_MINUTES)
    return list(close_times.loc[close_times > cutoff].iloc[:limit])


def setup_bar_context_passes(hist: pd.DataFrame, setup, direction: str) -> bool:
    """Validate context using data known before the NEXT_OPEN event."""
    enriched = add_indicators(hist, CFG)
    if setup.setup_index < 0 or setup.setup_index >= len(enriched):
        return False
    return context_ok(enriched.iloc[setup.setup_index], Side(direction))


def next_native_bar(candles: pd.DataFrame, setup_index: int):
    idx = setup_index + 1
    if idx < 0 or idx >= len(candles):
        return None, None
    return idx, candles.iloc[idx]


def simulate_from_open(plan, candles: pd.DataFrame, entry_index: int) -> dict:
    result = {
        "sim_exit": None,
        "sim_exit_reason": None,
        "sim_gross_pnl": None,
        "sim_costs": None,
        "sim_net_pnl": None,
    }
    if plan is None or entry_index is None or entry_index >= len(candles):
        return result

    future = candles.iloc[entry_index:].copy()
    future = future.loc[
        pd.to_datetime(future["date"]).dt.strftime("%Y-%m-%d") == SESSION_DATE
    ]
    if future.empty:
        result["sim_exit_reason"] = "NO_POST_ENTRY_BARS"
        return result

    side = plan.side.value
    exit_price = None
    exit_reason = None

    for _, bar in future.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        if side == "BUY":
            stop_hit = low <= plan.stop_price
            target_hit = high >= plan.target_price
        else:
            stop_hit = high >= plan.stop_price
            target_hit = low <= plan.target_price

        if stop_hit and target_hit:
            exit_price = plan.stop_price
            exit_reason = "BOTH_HIT_STOP_ASSUMED"
            break
        if stop_hit:
            exit_price = plan.stop_price
            exit_reason = "STOP"
            break
        if target_hit:
            exit_price = plan.target_price
            exit_reason = "TARGET_2R"
            break

    if exit_price is None:
        exit_price = float(future.iloc[-1]["close"])
        exit_reason = "EOD_CLOSE"

    pnl = net_pnl_for_trade(side, plan.quantity, plan.entry_price, exit_price)
    result.update(
        {
            "sim_exit": float(exit_price),
            "sim_exit_reason": exit_reason,
            "sim_gross_pnl": float(pnl["gross_pnl"]),
            "sim_costs": float(pnl["costs"]),
            "sim_net_pnl": float(pnl["net_pnl"]),
        }
    )
    return result


def try_true_next_open(symbol: str, direction: str, hist: pd.DataFrame,
                       candles: pd.DataFrame, engine: CandlestickEngine):
    """Execute a pending NEXT_OPEN setup at the real next native 10m open.

    Crucially, this function does not read next-bar high/low/close/volume before
    constructing the plan.  It uses only the next bar's timestamp and OPEN.
    """
    side = Side(direction)
    candidates = [
        s for s in engine.pending.get(symbol, [])
        if s.side == side and s.trigger == Trigger.NEXT_OPEN
        and s.setup_index == len(hist) - 1
    ]
    if not candidates:
        return None

    for setup in candidates:
        if not setup_bar_context_passes(hist, setup, direction):
            continue

        entry_index, next_bar = next_native_bar(candles, setup.setup_index)
        if next_bar is None:
            continue

        setup_bar = hist.iloc[setup.setup_index]
        setup_close_time = pd.Timestamp(setup_bar["date"]) + pd.Timedelta(minutes=BAR_MINUTES)
        next_open_time = pd.Timestamp(next_bar["date"])

        # Native 10m must make the setup fully known no later than next bar open.
        if next_open_time < setup_close_time:
            raise RuntimeError(
                f"LOOKAHEAD SAFETY ABORT {symbol}: next open {next_open_time} "
                f"precedes setup close {setup_close_time}"
            )
        if next_open_time.strftime("%Y-%m-%d") != SESSION_DATE:
            continue

        plan = build_trade_plan(
            setup,
            entry_index,
            float(next_bar["open"]),
            EQUITY,
            CFG,
        )
        if plan is not None:
            return {
                "plan": plan,
                "entry_index": entry_index,
                "entry_time": next_open_time,
                "setup_close_time": setup_close_time,
            }
    return None


def replay_trade(trade: dict, candles: pd.DataFrame, tick_size: float) -> dict:
    symbol = str(trade.get("symbol") or "")
    exchange = str(trade.get("exchange") or "NSE")
    direction = str(trade.get("direction") or "").upper().strip()
    sig_ts, ts_source = signal_timestamp(trade)

    out = {
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "signal_timestamp": sig_ts.isoformat() if sig_ts is not None else None,
        "timestamp_source": ts_source,
        "actual_pnl": finite_or_none(trade.get("pnl")),
        "actual_entry": finite_or_none(trade.get("entry")),
        "final_outcome": None,
        "initial_state": None,
        "initial_pattern": None,
        "initial_trigger": None,
        "confirmed_pattern": None,
        "confirmed_trigger": None,
        "confirm_delay_bars": None,
        "entry_time": None,
        "plan_entry": None,
        "plan_stop": None,
        "plan_target": None,
        "quantity": None,
        "planned_risk": None,
        "rr": None,
        "sim_exit": None,
        "sim_exit_reason": None,
        "sim_net_pnl": None,
        "error": None,
    }

    if sig_ts is None:
        out["final_outcome"] = "UNRESOLVED_TIMESTAMP"
        return out
    if direction not in {"BUY", "SELL"}:
        out["final_outcome"] = "INVALID_DIRECTION"
        return out

    hist = completed_slice(candles, sig_ts)
    if len(hist) < WARMUP_BARS:
        out["final_outcome"] = "INSUFFICIENT_HISTORY"
        return out

    engine = CandlestickEngine(CFG)
    first = evaluate_trade_entry(symbol, hist, direction, EQUITY, tick_size, engine)
    out["initial_state"] = first.state.value
    out["initial_pattern"] = first.pattern.value if first.pattern is not None else None
    out["initial_trigger"] = first.setup.trigger.value if first.setup is not None else None

    if first.state == GateState.CONFIRMED:
        plan = first.plan
        entry_index = plan.entry_index
        entry_time = pd.Timestamp(hist.iloc[entry_index]["date"]) + pd.Timedelta(minutes=BAR_MINUTES)
        out["final_outcome"] = "CONFIRMED_IMMEDIATE"
        delay = 0
    elif first.state == GateState.NO_PATTERN:
        out["final_outcome"] = "NO_PATTERN"
        return out
    else:
        # Before allowing a NEXT_OPEN setup to survive until bar close, schedule
        # it at the actual next native 10m open using only setup-bar information.
        scheduled = try_true_next_open(symbol, direction, hist, candles, engine)
        if scheduled is not None:
            plan = scheduled["plan"]
            entry_index = scheduled["entry_index"]
            entry_time = scheduled["entry_time"]
            out["final_outcome"] = "CONFIRMED_TRUE_NEXT_OPEN_1"
            delay = 1
        else:
            plan = None
            entry_index = None
            entry_time = None
            delay = None

            for wait_no, close_time in enumerate(
                next_completed_times(candles, sig_ts, CFG.max_wait_bars),
                start=1,
            ):
                step_hist = completed_slice(candles, close_time)

                # Remove any NEXT_OPEN setup that could not be executed at its
                # correct boundary; never let the normal engine confirm it later
                # using a completed next bar's open retrospectively.
                engine.pending[symbol] = [
                    s for s in engine.pending.get(symbol, [])
                    if s.trigger != Trigger.NEXT_OPEN
                ]

                step = evaluate_trade_entry(
                    symbol,
                    step_hist,
                    direction,
                    EQUITY,
                    tick_size,
                    engine,
                )

                if step.state == GateState.CONFIRMED:
                    plan = step.plan
                    entry_index = plan.entry_index
                    entry_time = pd.Timestamp(step_hist.iloc[entry_index]["date"]) + pd.Timedelta(minutes=BAR_MINUTES)
                    out["final_outcome"] = f"CONFIRMED_DELAYED_{wait_no}"
                    delay = wait_no
                    break

                # A new NEXT_OPEN pattern may have formed on this just-closed bar.
                if step.state == GateState.WAITING:
                    scheduled = try_true_next_open(
                        symbol, direction, step_hist, candles, engine
                    )
                    if scheduled is not None:
                        plan = scheduled["plan"]
                        entry_index = scheduled["entry_index"]
                        entry_time = scheduled["entry_time"]
                        out["final_outcome"] = f"CONFIRMED_TRUE_NEXT_OPEN_{wait_no + 1}"
                        delay = wait_no + 1
                        break

            if plan is None:
                out["final_outcome"] = "BLOCKED_AFTER_2_BARS"
                return out

    out["confirm_delay_bars"] = delay
    out["confirmed_pattern"] = plan.pattern.value
    out["confirmed_trigger"] = plan.trigger.value
    out["entry_time"] = entry_time.isoformat() if entry_time is not None else None
    out["plan_entry"] = plan.entry_price
    out["plan_stop"] = plan.stop_price
    out["plan_target"] = plan.target_price
    out["quantity"] = plan.quantity
    out["planned_risk"] = plan.planned_risk
    out["rr"] = plan.rr
    out.update(simulate_from_open(plan, candles, entry_index))
    return out


def print_summary(rows: list[dict]):
    outcomes = Counter(r.get("final_outcome") for r in rows)
    confirmed = [r for r in rows if str(r.get("final_outcome") or "").startswith("CONFIRMED")]
    safe = [r for r in confirmed if r.get("sim_net_pnl") is not None]
    actual_total = sum(r["actual_pnl"] for r in rows if r.get("actual_pnl") is not None)
    actual_confirmed = sum(r["actual_pnl"] for r in confirmed if r.get("actual_pnl") is not None)
    sim_net = sum(r["sim_net_pnl"] for r in safe)

    print("\n===== NATIVE 10m TRUE NEXT_OPEN REPLAY =====")
    print(f"Trades analysed: {len(rows)}")
    for key in sorted(outcomes):
        print(f"{key}: {outcomes[key]}")
    print(f"Confirmed total: {len(confirmed)}")
    print(f"Safely simulated confirmed trades: {len(safe)}")
    print(f"Actual Aug-12 baseline net P&L: {actual_total:.2f}")
    print(f"Baseline actual P&L of confirmed subset: {actual_confirmed:.2f}")
    print(f"Native-10m candlestick-plan simulated net P&L: {sim_net:.2f}")

    true_next = [r for r in confirmed if r.get("confirmed_trigger") == Trigger.NEXT_OPEN.value]
    print(f"True NEXT_OPEN confirmed trades: {len(true_next)}")

    print("\n===== FOCUS: RHIM / SENCO / FORTIS / SAPPHIRE / POLYPLEX =====")
    for r in rows:
        if r.get("symbol") not in FOCUS:
            continue
        print(
            f"{r['symbol']} {r['direction']} signal={r['signal_timestamp']} "
            f"initial={r['initial_state']}/{r['initial_pattern']}/{r['initial_trigger']} "
            f"outcome={r['final_outcome']} pattern={r['confirmed_pattern']} "
            f"entry_time={r['entry_time']} entry={r['plan_entry']} SL={r['plan_stop']} "
            f"TP={r['plan_target']} qty={r['quantity']} sim_net={r['sim_net_pnl']} "
            f"baseline_actual={r['actual_pnl']}"
        )

    print("\n===== CONFIRMED DETAIL =====")
    if not confirmed:
        print("No confirmed native-10m trades.")
    for r in confirmed:
        print(
            f"{r['symbol']} {r['direction']} {r['final_outcome']} "
            f"{r['confirmed_pattern']} trigger={r['confirmed_trigger']} | "
            f"entry={r['plan_entry']} SL={r['plan_stop']} TP={r['plan_target']} "
            f"qty={r['quantity']} exit={r['sim_exit']} reason={r['sim_exit_reason']} "
            f"sim_net={r['sim_net_pnl']} baseline_actual={r['actual_pnl']}"
        )


def main():
    print("READ-ONLY MODE: native Kite 10minute historical candles; no orders; no state writes.")
    print("NEXT_OPEN is executed at the actual next native 10m bar OPEN with no next-bar look-ahead.")
    print("Risk=0.20%, minRR=2.0, strict completed setup-bar VWAP+EMA50 context.")

    trades = load_aug12_trades()
    if not trades:
        raise SystemExit(f"No Aug-12 trades found in {LOG_PATH}")

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
        tick = tick_map.get(key, 0.05)

        if not token:
            row = {
                "symbol": symbol,
                "exchange": exchange,
                "direction": str(trade.get("direction") or ""),
                "actual_pnl": finite_or_none(trade.get("pnl")),
                "final_outcome": "INSTRUMENT_NOT_FOUND",
                "error": f"No instrument token for {exchange}:{symbol}",
            }
            rows.append(row)
            print(f"[{idx}/{len(trades)}] {exchange}:{symbol} -> INSTRUMENT_NOT_FOUND")
            continue

        if key not in cache:
            try:
                cache[key] = fetch_native_10m(kite, token)
            except Exception as exc:
                cache[key] = pd.DataFrame()
                print(f"Native 10m fetch failed for {exchange}:{symbol}: {exc}")

        try:
            row = replay_trade(trade, cache[key], tick)
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
