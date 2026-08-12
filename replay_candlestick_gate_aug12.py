"""Read-only Aug-12 candlestick-gate replay: 3m baseline vs 5m resample.

This harness keeps ``candlestick_engine.py`` untouched.  It replays the actual
Aug-12 PAPER trades using each trade's already-decided BUY/SELL direction as the
upstream intended_direction and compares:

1) the original 3-minute candle geometry; and
2) a 5-minute OHLCV view resampled from the historical 3-minute bars.

5-minute aggregation rule:
    open   = first
    high   = max
    low    = min
    close  = last
    volume = sum

The candlestick engine recalculates EMA50, session VWAP, prior-20-bar volume SMA
and average-body statistics from whichever OHLCV dataframe is supplied to it.
Thus the 5m pass uses 5m-native indicators derived from the resampled OHLCV.

IMPORTANT QUANT NOTE
--------------------
Three-minute bars do not partition exactly into five-minute bars.  Therefore a
3m->5m resample cannot reconstruct a broker-native 5m candle perfectly.  This
script records the latest completion time of every contributing 3m source bar
(`available_at`) and refuses to expose a derived 5m bar before all contributing
3m bars have closed.  That prevents look-ahead, but the resulting geometry is
still an approximation.  A production-grade conclusion should be verified with
Kite's native ``5minute`` historical candles before deployment.

Safety / interpretation:
- Historical Kite reads only; no broker orders are submitted.
- No production state files are written.
- Output CSV is written only to /tmp.
- PAPER risk remains 0.20%; minimum target remains 2R; pending lifetime is 2 bars.
- Strict BUY context remains close > VWAP AND close > EMA50; SELL is the mirror.
- The engine's volume rule remains unchanged.
- NEXT_OPEN plans remain flagged as semantic-risk rows and are excluded from the
  simulated P&L because the current engine learns that bar only after it closes.
- Simulated P&L below is a simple candlestick-plan stop/2R-target path, not a full
  replay of CP9/hybrid exits.  If stop and target occur in the same OHLC bar, the
  conservative assumption is STOP first.  If neither is hit by EOD, the position
  is marked to the final available close.  Estimated Zerodha costs are deducted.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict

import pandas as pd

from auth import get_kite_client
from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Trigger,
    evaluate_trade_entry,
)
from costs import net_pnl_for_trade
from trade_log import LOG_PATH

SESSION_DATE = "2026-08-12"
IST = "Asia/Kolkata"
SOURCE_INTERVAL = "3minute"
SOURCE_BAR_MINUTES = 3
FIVE_MINUTES = 5
FROM_TS = pd.Timestamp("2026-08-11 09:15", tz=IST)
TO_TS = pd.Timestamp("2026-08-12 15:30", tz=IST)
OUTPUT_CSV = "/tmp/candlestick_gate_aug12_3m_vs_5m.csv"
EQUITY = 5000.0
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)
WARMUP_BARS = max(CFG.volume_lookback, CFG.body_lookback) + 5


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
    # Prefer the exact completed signal-candle close recorded by the PAPER bot.
    for key in ("signal_candle_close", "entry_time"):
        ts = to_ist(trade.get(key), str(trade.get("date") or SESSION_DATE))
        if ts is not None:
            return ts, key
    return None, None


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
            tick = inst.get("tick_size")
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


def fetch_symbol_3m_candles(kite, token: int) -> pd.DataFrame:
    raw = kite.historical_data(
        token,
        FROM_TS.to_pydatetime(),
        TO_TS.to_pydatetime(),
        SOURCE_INTERVAL,
    )
    # Keep the replay gentle on Kite's historical endpoint.
    time.sleep(0.35)
    return normalize_candles(raw)


def resample_3m_to_5m(df_3m: pd.DataFrame) -> pd.DataFrame:
    """Resample normalized 3m OHLCV into a no-lookahead 5m approximation.

    The timestamps on Kite candles are bar-start timestamps.  We align bins to
    the NSE cash-session origin (09:15 IST), aggregate OHLCV exactly as specified,
    and carry ``available_at`` = max(source_start + 3 minutes).  Replay consumers
    use ``available_at`` rather than nominal 5m close time, so a 3m-derived bar is
    never visible before every source candle contributing to it has completed.
    """
    if df_3m is None or df_3m.empty:
        return pd.DataFrame()

    work = df_3m.copy().sort_values("date")
    work["source_available_at"] = work["date"] + pd.Timedelta(
        minutes=SOURCE_BAR_MINUTES
    )
    work = work.set_index("date")

    resampler_kwargs = dict(
        rule="5min",
        origin="start_day",
        offset="9h15min",
        label="left",
        closed="left",
    )

    bars = work.resample(**resampler_kwargs).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "source_available_at": "max",
        }
    )
    counts = work["open"].resample(**resampler_kwargs).count()

    bars["source_bar_count"] = counts
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars = bars.rename(columns={"source_available_at": "available_at"})
    bars = bars.reset_index()

    # Keep only normal NSE/BSE cash-session starts.  Empty overnight bins have
    # already been dropped; this also protects against accidental extended data.
    clock = bars["date"].dt.time
    session_start = pd.Timestamp("09:15").time()
    session_last_start = pd.Timestamp("15:25").time()
    bars = bars.loc[(clock >= session_start) & (clock <= session_last_start)]

    return bars.reset_index(drop=True)


def bar_available_times(df: pd.DataFrame, bar_minutes: int) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="datetime64[ns]")
    if "available_at" in df.columns:
        return pd.to_datetime(df["available_at"])
    return pd.to_datetime(df["date"]) + pd.Timedelta(minutes=bar_minutes)


def completed_slice(df: pd.DataFrame, cutoff, bar_minutes: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    available = bar_available_times(df, bar_minutes)
    return df.loc[available <= cutoff].copy().reset_index(drop=True)


def next_completed_bars(
    df: pd.DataFrame,
    cutoff,
    bar_minutes: int,
    limit: int = 2,
) -> list[pd.Timestamp]:
    available = bar_available_times(df, bar_minutes)
    return list(available.loc[available > cutoff].drop_duplicates().iloc[:limit])


def finite_or_none(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def session_rows_after(
    candles: pd.DataFrame,
    after_time,
    bar_minutes: int,
) -> pd.DataFrame:
    """Return subsequent bars from Aug 12 after a confirmed close-time entry."""
    if candles is None or candles.empty or after_time is None:
        return pd.DataFrame()
    starts = pd.to_datetime(candles["date"])
    same_day = starts.dt.strftime("%Y-%m-%d") == SESSION_DATE
    # Entry plans from CLOSED_CANDLE/BREAKOUT are created at the completed-bar
    # information boundary.  Only a later bar can hit the new stop/target.
    later = starts >= pd.Timestamp(after_time)
    available = bar_available_times(candles, bar_minutes)
    visible = available > pd.Timestamp(after_time)
    return candles.loc[same_day & later & visible].copy().reset_index(drop=True)


def simulate_plan_pnl(plan, candles: pd.DataFrame, entry_time, bar_minutes: int) -> dict:
    """Conservative OHLC path for the candlestick plan only.

    This intentionally does not emulate CP9, MAE/MFE, hybrid exits or the real
    PAPER executor.  It tests only the engine's geometric stop and minimum-2R TP.
    """
    result = {
        "sim_exit": None,
        "sim_exit_reason": None,
        "sim_gross_pnl": None,
        "sim_costs": None,
        "sim_net_pnl": None,
    }
    if plan is None:
        return result
    if plan.trigger == Trigger.NEXT_OPEN:
        result["sim_exit_reason"] = "UNSAFE_NEXT_OPEN_EXCLUDED"
        return result

    future = session_rows_after(candles, entry_time, bar_minutes)
    if future.empty:
        result["sim_exit_reason"] = "NO_POST_ENTRY_BARS"
        return result

    exit_price = None
    exit_reason = None
    side = plan.side.value

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
            # OHLC cannot identify intrabar ordering.  Use conservative stop-first.
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


def attach_plan_fields(
    base: dict,
    plan,
    hist: pd.DataFrame,
    all_candles: pd.DataFrame,
    bar_minutes: int,
):
    if plan is None:
        return

    base["confirmed_pattern"] = plan.pattern.value
    base["plan_entry"] = plan.entry_price
    base["plan_stop"] = plan.stop_price
    base["plan_target"] = plan.target_price
    base["planned_risk"] = plan.planned_risk
    base["quantity"] = plan.quantity
    base["rr"] = plan.rr
    base["confirmed_trigger"] = plan.trigger.value

    if 0 <= plan.entry_index < len(hist):
        entry_bar = hist.iloc[plan.entry_index]
        base["entry_bar_start"] = pd.Timestamp(entry_bar["date"]).isoformat()
        entry_times = bar_available_times(hist, bar_minutes)
        entry_time = pd.Timestamp(entry_times.iloc[plan.entry_index])
        base["entry_decision_time"] = entry_time.isoformat()
    else:
        entry_time = None

    if plan.trigger == Trigger.NEXT_OPEN:
        base["next_open_semantics"] = True

    base.update(simulate_plan_pnl(plan, all_candles, entry_time, bar_minutes))


def replay_trade(
    trade: dict,
    candles: pd.DataFrame,
    tick_size: float,
    timeframe: str,
    bar_minutes: int,
) -> dict:
    symbol = str(trade.get("symbol") or "")
    exchange = str(trade.get("exchange") or "NSE")
    direction = str(trade.get("direction") or "").upper().strip()
    sig_ts, ts_source = signal_timestamp(trade)

    base = {
        "timeframe": timeframe,
        "bar_minutes": bar_minutes,
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
        "confirmed_trigger": None,
        "plan_entry": None,
        "plan_stop": None,
        "plan_target": None,
        "quantity": None,
        "planned_risk": None,
        "rr": None,
        "entry_bar_start": None,
        "entry_decision_time": None,
        "next_open_semantics": False,
        "bars_at_signal": 0,
        "sim_exit": None,
        "sim_exit_reason": None,
        "sim_gross_pnl": None,
        "sim_costs": None,
        "sim_net_pnl": None,
        "error": None,
    }

    if sig_ts is None:
        base["final_outcome"] = "UNRESOLVED_TIMESTAMP"
        return base
    if direction not in {"BUY", "SELL"}:
        base["final_outcome"] = "INVALID_DIRECTION"
        return base

    hist = completed_slice(candles, sig_ts, bar_minutes)
    base["bars_at_signal"] = len(hist)
    if len(hist) < WARMUP_BARS:
        base["final_outcome"] = "INSUFFICIENT_HISTORY"
        return base

    engine = CandlestickEngine(CFG)
    result = evaluate_trade_entry(symbol, hist, direction, EQUITY, tick_size, engine)
    base["initial_state"] = result.state.value
    if result.pattern is not None:
        base["initial_pattern"] = result.pattern.value
    if result.setup is not None:
        base["initial_trigger"] = result.setup.trigger.value
        if result.setup.trigger == Trigger.NEXT_OPEN:
            base["next_open_semantics"] = True

    if result.state == GateState.CONFIRMED:
        base["final_outcome"] = "CONFIRMED_IMMEDIATE"
        base["confirm_delay_bars"] = 0
        attach_plan_fields(base, result.plan, hist, candles, bar_minutes)
        return base

    if result.state == GateState.NO_PATTERN:
        base["final_outcome"] = "NO_PATTERN"
        return base

    # Existing WAITING setup: preserve the engine's 2-bar pending lifetime.
    for delay, next_time in enumerate(
        next_completed_bars(candles, sig_ts, bar_minutes, limit=CFG.max_wait_bars),
        start=1,
    ):
        step_hist = completed_slice(candles, next_time, bar_minutes)
        step = evaluate_trade_entry(
            symbol,
            step_hist,
            direction,
            EQUITY,
            tick_size,
            engine,
        )
        if step.state == GateState.CONFIRMED:
            base["final_outcome"] = f"CONFIRMED_DELAYED_{delay}"
            base["confirm_delay_bars"] = delay
            attach_plan_fields(base, step.plan, step_hist, candles, bar_minutes)
            return base

    base["final_outcome"] = "BLOCKED_AFTER_2_BARS"
    return base


def is_confirmed(row: dict) -> bool:
    return str(row.get("final_outcome") or "").startswith("CONFIRMED")


def pct(count: int, total: int) -> float:
    return (100.0 * count / total) if total else 0.0


def summary_metrics(rows: list[dict], timeframe: str) -> dict:
    subset = [r for r in rows if r.get("timeframe") == timeframe]
    total = len(subset)
    no_pattern = sum(r.get("final_outcome") == "NO_PATTERN" for r in subset)
    blocked = sum(
        r.get("final_outcome") == "BLOCKED_AFTER_2_BARS" for r in subset
    )
    confirmed = sum(is_confirmed(r) for r in subset)
    other = total - no_pattern - blocked - confirmed
    actual_total = sum(
        r["actual_pnl"] for r in subset if r.get("actual_pnl") is not None
    )
    confirmed_actual = sum(
        r["actual_pnl"]
        for r in subset
        if is_confirmed(r) and r.get("actual_pnl") is not None
    )
    sim_rows = [
        r
        for r in subset
        if is_confirmed(r) and r.get("sim_net_pnl") is not None
    ]
    sim_net = sum(r["sim_net_pnl"] for r in sim_rows)
    return {
        "timeframe": timeframe,
        "total": total,
        "no_pattern": no_pattern,
        "blocked": blocked,
        "confirmed": confirmed,
        "other": other,
        "actual_total": actual_total,
        "confirmed_actual": confirmed_actual,
        "simulated_count": len(sim_rows),
        "simulated_net": sim_net,
    }


def print_metric_row(m: dict):
    total = m["total"]
    print(
        f"{m['timeframe']}: total={total} | "
        f"NO_PATTERN={m['no_pattern']} ({pct(m['no_pattern'], total):.2f}%) | "
        f"BLOCKED_AFTER_2_BARS={m['blocked']} ({pct(m['blocked'], total):.2f}%) | "
        f"CONFIRMED={m['confirmed']} ({pct(m['confirmed'], total):.2f}%) | "
        f"OTHER={m['other']} ({pct(m['other'], total):.2f}%)"
    )


def print_focus(rows: list[dict]):
    print("\n===== SAPPHIRE / POLYPLEX: 3m vs 5m =====")
    focus = [r for r in rows if r.get("symbol") in {"SAPPHIRE", "POLYPLEX"}]
    if not focus:
        print("No matching Aug-12 trade-history rows found.")
        return

    for r in focus:
        print(
            f"{r['timeframe']} | {r['symbol']} {r['direction']} "
            f"signal={r['signal_timestamp']} "
            f"initial={r['initial_state']}/{r['initial_pattern']} "
            f"outcome={r['final_outcome']} delay={r['confirm_delay_bars']} "
            f"pattern={r['confirmed_pattern']} trigger={r['confirmed_trigger']} "
            f"entry={r['plan_entry']} SL={r['plan_stop']} TP={r['plan_target']} "
            f"sim_net={r['sim_net_pnl']} baseline_actual={r['actual_pnl']}"
        )


def print_confirmed_comparison(rows: list[dict]):
    print("\n===== CONFIRMED-TRADE P&L COMPARISON =====")
    print(
        "Simulated P&L = candlestick plan only (geometric SL / 2R TP / EOD close, "
        "estimated costs). It is NOT the full PAPER exit-stack replay."
    )
    confirmed = [r for r in rows if is_confirmed(r)]
    if not confirmed:
        print("No CONFIRMED trades in either timeframe.")
        return

    for r in confirmed:
        print(
            f"{r['timeframe']} | {r['symbol']} {r['direction']} "
            f"{r['final_outcome']} {r['confirmed_pattern']} | "
            f"entry={r['plan_entry']:.4f} SL={r['plan_stop']:.4f} "
            f"TP={r['plan_target']:.4f} qty={r['quantity']} | "
            f"sim_exit={r['sim_exit']} reason={r['sim_exit_reason']} "
            f"sim_net={r['sim_net_pnl']} | baseline_actual={r['actual_pnl']}"
        )


def print_summary(rows: list[dict]):
    m3 = summary_metrics(rows, "3m")
    m5 = summary_metrics(rows, "5m_resampled")

    print("\n===== AUG-12 CANDLESTICK GATE: 3m vs 5m =====")
    print_metric_row(m3)
    print_metric_row(m5)

    print("\n===== TIMEFRAME DELTA =====")
    print(f"Confirmed delta: {m5['confirmed'] - m3['confirmed']:+d} trades")
    print(f"NO_PATTERN delta: {m5['no_pattern'] - m3['no_pattern']:+d} trades")
    print(
        "BLOCKED_AFTER_2_BARS delta: "
        f"{m5['blocked'] - m3['blocked']:+d} trades"
    )

    print("\n===== BASELINE / SIMULATED P&L SUMMARY =====")
    print(
        f"Actual Aug-12 baseline net P&L: {m3['actual_total']:.2f} "
        "(same 75 trades for both timeframe classifications)"
    )
    for m in (m3, m5):
        print(
            f"{m['timeframe']}: confirmed={m['confirmed']} | "
            f"baseline actual P&L of confirmed subset={m['confirmed_actual']:.2f} | "
            f"simulated safe confirmed trades={m['simulated_count']} | "
            f"candlestick-plan simulated net P&L={m['simulated_net']:.2f}"
        )

    next_open = [r for r in rows if r.get("next_open_semantics")]
    print(f"\nNEXT_OPEN semantic-risk rows across both passes: {len(next_open)}")
    print("NEXT_OPEN rows are excluded from simulated P&L until a true bar-boundary execution path exists.")

    print_focus(rows)
    print_confirmed_comparison(rows)


def main():
    print("READ-ONLY MODE: historical data only; no orders; no production-state writes.")
    print("Engine unchanged: risk=0.20%, minRR=2.0, pending lifetime=2 bars.")
    print(
        "WARNING: 3m->5m resampling is an approximation because 3 and 5 minute "
        "boundaries are incommensurate; available_at prevents look-ahead."
    )

    trades = load_aug12_trades()
    if not trades:
        raise SystemExit(f"No trades found for {SESSION_DATE} in {LOG_PATH}")
    print(f"Loaded {len(trades)} Aug-12 trade-history rows from {LOG_PATH}")

    kite = get_kite_client()
    exchanges = {str(t.get("exchange") or "NSE") for t in trades}
    token_map, tick_map = build_instrument_maps(kite, exchanges)

    source_cache = {}
    five_cache = {}
    rows = []

    for idx, trade in enumerate(trades, start=1):
        symbol = str(trade.get("symbol") or "")
        exchange = str(trade.get("exchange") or "NSE")
        key = (exchange, symbol)
        token = token_map.get(key)
        tick = tick_map.get(key, 0.05)

        if not token:
            for timeframe, minutes in (("3m", SOURCE_BAR_MINUTES), ("5m_resampled", FIVE_MINUTES)):
                row = replay_trade(trade, pd.DataFrame(), tick, timeframe, minutes)
                row["final_outcome"] = "INSTRUMENT_NOT_FOUND"
                row["error"] = f"No instrument token for {exchange}:{symbol}"
                rows.append(row)
            print(f"[{idx}/{len(trades)}] {exchange}:{symbol} -> INSTRUMENT_NOT_FOUND")
            continue

        if key not in source_cache:
            try:
                source_cache[key] = fetch_symbol_3m_candles(kite, token)
                five_cache[key] = resample_3m_to_5m(source_cache[key])
            except Exception as exc:
                source_cache[key] = pd.DataFrame()
                five_cache[key] = pd.DataFrame()
                print(f"Historical fetch/resample failed for {exchange}:{symbol}: {exc}")

        pair = []
        for timeframe, candles, minutes in (
            ("3m", source_cache[key], SOURCE_BAR_MINUTES),
            ("5m_resampled", five_cache[key], FIVE_MINUTES),
        ):
            try:
                row = replay_trade(trade, candles, tick, timeframe, minutes)
            except Exception as exc:
                row = {
                    "timeframe": timeframe,
                    "bar_minutes": minutes,
                    "symbol": symbol,
                    "exchange": exchange,
                    "direction": str(trade.get("direction") or ""),
                    "actual_pnl": finite_or_none(trade.get("pnl")),
                    "final_outcome": "ERROR",
                    "error": repr(exc),
                }
            rows.append(row)
            pair.append(f"{timeframe}={row.get('final_outcome')}")

        print(f"[{idx}/{len(trades)}] {exchange}:{symbol} {trade.get('direction')} | " + " | ".join(pair))

    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False)
    print_summary(rows)
    print(f"\nDetailed CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
