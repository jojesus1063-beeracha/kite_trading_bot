"""Read-only Aug-12 candlestick-gate replay across 3m, 5m, 10m and 15m.

The candlestick engine itself is intentionally untouched.  This harness replays
all actual Aug-12 PAPER trades using the already-decided BUY/SELL direction as
``intended_direction`` and compares four entry-candle views:

- 3m: original Kite historical bars
- 5m: resampled from 3m
- 10m: resampled from 3m
- 15m: resampled from 3m

Resampling rule for every derived timeframe:
    open   = first
    high   = max
    low    = min
    close  = last
    volume = sum

The engine recalculates its own EMA50, session VWAP, prior-20-bar volume SMA and
average-body statistics from whichever OHLCV dataframe is supplied.  Therefore
5m/10m/15m evaluations use timeframe-native indicators derived from the
resampled OHLCV.

NO-LOOKAHEAD / AGGREGATION NOTE
-------------------------------
Kite timestamps each 3m candle by its start time.  Derived bars carry
``available_at`` = the latest close time among all contributing 3m source bars.
The replay never exposes a resampled candle before every source candle used to
build it has completed.

15m aligns exactly with 3m when anchored at 09:15 IST (five 3m bars per 15m).
5m and 10m do NOT partition exactly into 3m bars, so those two geometries are
approximations.  They remain useful for research, but production conclusions
should be verified against native Kite 5minute/10minute data where available.

Safety / interpretation:
- Historical Kite reads only; no broker orders are submitted.
- No production state files are written.
- Output CSV is written only to /tmp.
- PAPER risk remains 0.20%; minimum target remains 2R.
- Pending lifetime remains max 2 completed bars.
- Strict BUY context remains close > VWAP AND close > EMA50; SELL is the mirror.
- The engine's strict completed-bar Volume > Volume SMA20 rule is unchanged.
- NEXT_OPEN plans remain flagged and excluded from simulated P&L because current
  engine semantics learn the next bar only after it closes.
- Simulated P&L uses only the candlestick plan's geometric stop / 2R target / EOD
  close, with estimated costs.  It is not a full CP9/hybrid-exit replay.
- If stop and target are both inside one OHLC bar, STOP is assumed first.
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
    Trigger,
    evaluate_trade_entry,
)
from costs import net_pnl_for_trade
from trade_log import LOG_PATH

SESSION_DATE = "2026-08-12"
IST = "Asia/Kolkata"
SOURCE_INTERVAL = "3minute"
SOURCE_BAR_MINUTES = 3
FROM_TS = pd.Timestamp("2026-08-11 09:15", tz=IST)
TO_TS = pd.Timestamp("2026-08-12 15:30", tz=IST)
OUTPUT_CSV = "/tmp/candlestick_gate_aug12_3m_5m_10m_15m.csv"
EQUITY = 5000.0
CFG = EngineConfig(risk_pct=0.20, min_rr=2.0, max_wait_bars=2)
WARMUP_BARS = max(CFG.volume_lookback, CFG.body_lookback) + 5

TIMEFRAMES = (
    ("3m", 3, False),
    ("5m_resampled", 5, True),
    ("10m_resampled", 10, True),
    ("15m_resampled", 15, True),
)

FOCUS_SYMBOLS = {"SAPPHIRE", "POLYPLEX", "FORTIS"}


def load_aug12_trades(path: str = LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Trade history not found: {path}")

    trades = []
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
    # Prefer the exact completed signal-candle close recorded by PAPER.
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

            try:
                tick = float(inst.get("tick_size"))
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
    # Keep historical requests gentle on Kite.
    time.sleep(0.35)
    return normalize_candles(raw)


def resample_3m(df_3m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample normalized 3m OHLCV to ``minutes`` with no look-ahead.

    Bars are anchored to the 09:15 IST cash-session origin.  ``available_at`` is
    the latest source 3m close contributing to each derived candle, and replay
    visibility is based on that timestamp rather than merely the nominal bucket
    end.
    """
    if minutes <= SOURCE_BAR_MINUTES:
        raise ValueError("resample target must be greater than 3 minutes")
    if df_3m is None or df_3m.empty:
        return pd.DataFrame()

    work = df_3m.copy().sort_values("date")
    work["source_available_at"] = work["date"] + pd.Timedelta(
        minutes=SOURCE_BAR_MINUTES
    )
    work = work.set_index("date")

    kwargs = dict(
        rule=f"{minutes}min",
        origin="start_day",
        offset="9h15min",
        label="left",
        closed="left",
    )

    bars = work.resample(**kwargs).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "source_available_at": "max",
        }
    )
    bars["source_bar_count"] = work["open"].resample(**kwargs).count()
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars = bars.rename(columns={"source_available_at": "available_at"})
    bars = bars.reset_index()

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
    if df is None or df.empty:
        return []
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
    if candles is None or candles.empty or after_time is None:
        return pd.DataFrame()

    starts = pd.to_datetime(candles["date"])
    same_day = starts.dt.strftime("%Y-%m-%d") == SESSION_DATE
    available = bar_available_times(candles, bar_minutes)

    # Only bars whose information arrives after the entry decision may hit a new
    # candlestick-plan stop/target.
    visible = available > pd.Timestamp(after_time)
    return candles.loc[same_day & visible].copy().reset_index(drop=True)


def simulate_plan_pnl(plan, candles: pd.DataFrame, entry_time, bar_minutes: int) -> dict:
    """Conservative OHLC simulation for the candlestick plan only."""
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
    base["confirmed_trigger"] = plan.trigger.value
    base["plan_entry"] = plan.entry_price
    base["plan_stop"] = plan.stop_price
    base["plan_target"] = plan.target_price
    base["planned_risk"] = plan.planned_risk
    base["quantity"] = plan.quantity
    base["rr"] = plan.rr

    entry_time = None
    if 0 <= plan.entry_index < len(hist):
        entry_bar = hist.iloc[plan.entry_index]
        base["entry_bar_start"] = pd.Timestamp(entry_bar["date"]).isoformat()
        entry_times = bar_available_times(hist, bar_minutes)
        entry_time = pd.Timestamp(entry_times.iloc[plan.entry_index])
        base["entry_decision_time"] = entry_time.isoformat()

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

    for delay, next_time in enumerate(
        next_completed_bars(
            candles,
            sig_ts,
            bar_minutes,
            limit=CFG.max_wait_bars,
        ),
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
    insufficient = sum(
        r.get("final_outcome") == "INSUFFICIENT_HISTORY" for r in subset
    )
    other = total - no_pattern - blocked - confirmed - insufficient

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
        "insufficient": insufficient,
        "other": other,
        "actual_total": actual_total,
        "confirmed_actual": confirmed_actual,
        "simulated_count": len(sim_rows),
        "simulated_net": sim_net,
    }


def print_summary_table(metrics: list[dict]):
    print("\n===== AUG-12 CANDLESTICK GATE MATRIX =====")
    print(
        f"{'TIMEFRAME':<16} {'TOTAL':>5} {'NO_PATTERN':>18} "
        f"{'EXPIRED_WAIT':>18} {'CONFIRMED':>18} {'SIM_NET':>11}"
    )
    print("-" * 92)

    for m in metrics:
        total = m["total"]
        print(
            f"{m['timeframe']:<16} {total:>5} "
            f"{m['no_pattern']:>5} ({pct(m['no_pattern'], total):>6.2f}%) "
            f"{m['blocked']:>5} ({pct(m['blocked'], total):>6.2f}%) "
            f"{m['confirmed']:>5} ({pct(m['confirmed'], total):>6.2f}%) "
            f"{m['simulated_net']:>11.2f}"
        )

    if any(m["insufficient"] for m in metrics):
        print("\nINSUFFICIENT_HISTORY by timeframe:")
        for m in metrics:
            if m["insufficient"]:
                print(f"  {m['timeframe']}: {m['insufficient']}")


def print_pnl_summary(metrics: list[dict]):
    baseline = metrics[0]["actual_total"] if metrics else 0.0

    print("\n===== BASELINE / SIMULATED P&L SUMMARY =====")
    print(f"Actual Aug-12 baseline net P&L: {baseline:.2f}")
    print(
        "Simulated net P&L below includes only safe CONFIRMED candlestick-plan "
        "trades; blocked/no-pattern trades contribute zero by design."
    )

    for m in metrics:
        print(
            f"{m['timeframe']}: confirmed={m['confirmed']} | "
            f"baseline actual P&L of confirmed subset={m['confirmed_actual']:.2f} | "
            f"safe simulated trades={m['simulated_count']} | "
            f"simulated net P&L={m['simulated_net']:.2f} | "
            f"vs baseline={m['simulated_net'] - baseline:+.2f}"
        )


def print_focus(rows: list[dict]):
    print("\n===== SAPPHIRE / POLYPLEX / FORTIS BY TIMEFRAME =====")
    focus = [r for r in rows if r.get("symbol") in FOCUS_SYMBOLS]
    if not focus:
        print("No focus rows found.")
        return

    tf_order = {name: i for i, (name, _, _) in enumerate(TIMEFRAMES)}
    focus.sort(
        key=lambda r: (
            r.get("symbol") or "",
            r.get("signal_timestamp") or "",
            tf_order.get(r.get("timeframe"), 999),
        )
    )

    for r in focus:
        print(
            f"{r['timeframe']:<15} | {r['symbol']} {r['direction']} "
            f"signal={r['signal_timestamp']} | "
            f"initial={r['initial_state']}/{r['initial_pattern']} | "
            f"outcome={r['final_outcome']} delay={r['confirm_delay_bars']} | "
            f"pattern={r['confirmed_pattern']} trigger={r['confirmed_trigger']} | "
            f"entry={r['plan_entry']} SL={r['plan_stop']} TP={r['plan_target']} "
            f"qty={r['quantity']} | sim_net={r['sim_net_pnl']} | "
            f"baseline_actual={r['actual_pnl']}"
        )


def print_confirmed_comparison(rows: list[dict]):
    print("\n===== CONFIRMED-TRADE DETAIL =====")
    print(
        "Simulated P&L = candlestick plan only (geometric SL / 2R TP / EOD close, "
        "estimated costs); not the full PAPER exit stack."
    )

    confirmed = [r for r in rows if is_confirmed(r)]
    if not confirmed:
        print("No CONFIRMED trades in any timeframe.")
        return

    for r in confirmed:
        entry = "None" if r.get("plan_entry") is None else f"{r['plan_entry']:.4f}"
        stop = "None" if r.get("plan_stop") is None else f"{r['plan_stop']:.4f}"
        target = "None" if r.get("plan_target") is None else f"{r['plan_target']:.4f}"
        print(
            f"{r['timeframe']} | {r['symbol']} {r['direction']} "
            f"{r['final_outcome']} {r['confirmed_pattern']} | "
            f"entry={entry} SL={stop} TP={target} qty={r['quantity']} | "
            f"sim_exit={r['sim_exit']} reason={r['sim_exit_reason']} "
            f"sim_net={r['sim_net_pnl']} | baseline_actual={r['actual_pnl']}"
        )


def print_diagnostics(rows: list[dict]):
    metrics = [summary_metrics(rows, name) for name, _, _ in TIMEFRAMES]
    print_summary_table(metrics)
    print_pnl_summary(metrics)

    next_open = [r for r in rows if r.get("next_open_semantics")]
    print(f"\nNEXT_OPEN semantic-risk rows across all passes: {len(next_open)}")
    print(
        "NEXT_OPEN rows are excluded from simulated P&L until a true bar-boundary "
        "execution path exists."
    )

    print_focus(rows)
    print_confirmed_comparison(rows)


def main():
    print("READ-ONLY MODE: historical data only; no orders; no production-state writes.")
    print("Engine unchanged: risk=0.20%, minRR=2.0, pending lifetime=2 bars.")
    print(
        "Matrix: native 3m plus 5m/10m/15m OHLCV resamples from 3m; "
        "available_at prevents source-bar look-ahead."
    )
    print(
        "NOTE: 15m is exactly composable from aligned 3m bars; 5m and 10m are "
        "approximate because their boundaries are incommensurate with 3m."
    )

    trades = load_aug12_trades()
    if not trades:
        raise SystemExit(f"No trades found for {SESSION_DATE} in {LOG_PATH}")
    print(f"Loaded {len(trades)} Aug-12 trade-history rows from {LOG_PATH}")

    kite = get_kite_client()
    exchanges = {str(t.get("exchange") or "NSE") for t in trades}
    token_map, tick_map = build_instrument_maps(kite, exchanges)

    source_cache: dict[tuple[str, str], pd.DataFrame] = {}
    timeframe_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    rows: list[dict] = []

    for idx, trade in enumerate(trades, start=1):
        symbol = str(trade.get("symbol") or "")
        exchange = str(trade.get("exchange") or "NSE")
        key = (exchange, symbol)
        token = token_map.get(key)
        tick = tick_map.get(key, 0.05)

        if not token:
            for timeframe, minutes, _ in TIMEFRAMES:
                row = replay_trade(
                    trade,
                    pd.DataFrame(),
                    tick,
                    timeframe,
                    minutes,
                )
                row["final_outcome"] = "INSTRUMENT_NOT_FOUND"
                row["error"] = f"No instrument token for {exchange}:{symbol}"
                rows.append(row)
            print(f"[{idx}/{len(trades)}] {exchange}:{symbol} -> INSTRUMENT_NOT_FOUND")
            continue

        if key not in source_cache:
            try:
                source = fetch_symbol_3m_candles(kite, token)
                source_cache[key] = source
                timeframe_cache[(exchange, symbol, "3m")] = source
                timeframe_cache[(exchange, symbol, "5m_resampled")] = resample_3m(
                    source, 5
                )
                timeframe_cache[(exchange, symbol, "10m_resampled")] = resample_3m(
                    source, 10
                )
                timeframe_cache[(exchange, symbol, "15m_resampled")] = resample_3m(
                    source, 15
                )
            except Exception as exc:
                source_cache[key] = pd.DataFrame()
                for timeframe, _, _ in TIMEFRAMES:
                    timeframe_cache[(exchange, symbol, timeframe)] = pd.DataFrame()
                print(f"Historical fetch/resample failed for {exchange}:{symbol}: {exc}")

        line = []
        for timeframe, minutes, _ in TIMEFRAMES:
            candles = timeframe_cache.get(
                (exchange, symbol, timeframe),
                pd.DataFrame(),
            )
            try:
                row = replay_trade(
                    trade,
                    candles,
                    tick,
                    timeframe,
                    minutes,
                )
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
            line.append(f"{timeframe}={row.get('final_outcome')}")

        print(
            f"[{idx}/{len(trades)}] {exchange}:{symbol} {trade.get('direction')} | "
            + " | ".join(line)
        )

    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False)
    print_diagnostics(rows)
    print(f"\nDetailed CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
