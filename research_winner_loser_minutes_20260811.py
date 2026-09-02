#!/usr/bin/env python3
"""Minute-by-minute research of Aug-11 current-policy PAPER winners vs losers.

ANALYSIS ONLY. This module never imports/runs main.py and never places orders.

It reconstructs the same accepted trades as replay_current_policy_20260811.py,
then studies the path of every accepted trade from order submission to replay
exit.  It deliberately separates:

* the stored 15-minute ADX that authorized the entry;
* 1-minute diagnostic indicators used only for retrospective research;
* 3-minute entry-timeframe diagnostics; and
* completed 15-minute trend-context diagnostics.

Outputs in /tmp:
  aug11_trade_fingerprints.csv
  aug11_minute_paths.csv
  aug11_winner_loser_checkpoint_comparison.csv
  aug11_threshold_diagnostics.csv

The sample is only one trading day (20 current-policy trades in the latest
replay), so the report is descriptive research, not a validated optimization.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

import config as cfg
import replay_current_policy_20260811 as current
from indicators import atr as calc_atr
from indicators import vwap as calc_vwap

base = current.base
SESSION_DATE = current.SESSION_DATE
IST = base.IST

OUT_TRADE = Path("/tmp/aug11_trade_fingerprints.csv")
OUT_MINUTE = Path("/tmp/aug11_minute_paths.csv")
OUT_COMPARE = Path("/tmp/aug11_winner_loser_checkpoint_comparison.csv")
OUT_THRESHOLDS = Path("/tmp/aug11_threshold_diagnostics.csv")

CHECKPOINTS = [0, 1, 2, 3, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60]

COMPARE_FEATURES = [
    "current_pct",
    "mfe_from_entry_pct",
    "mae_from_entry_pct",
    "giveback_from_entry_pct",
    "candle_body_directional_pct",
    "candle_range_pct",
    "ema_spread_directional_1m_pct",
    "ema9_slope_directional_1m_pct",
    "price_vs_vwap_directional_1m_pct",
    "rsi14_1m",
    "adx14_1m",
    "di_edge_directional_1m",
    "atr14_1m_pct",
    "volume_ratio20_1m",
    "ema_spread_directional_3m_pct",
    "price_vs_vwap_directional_3m_pct",
    "rsi14_3m",
    "adx14_3m",
    "volume_ratio20_3m",
    "adx14_15m",
    "price_vs_vwap_directional_15m_pct",
]


def _finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return math.nan
    return x if math.isfinite(x) else math.nan


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def _adx_bundle(df: pd.DataFrame, period: int = 14):
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_mask = (up_move > down_move) & (up_move > 0)
    minus_mask = (down_move > up_move) & (down_move > 0)
    plus_dm.loc[plus_mask] = up_move.loc[plus_mask]
    minus_dm.loc[minus_mask] = down_move.loc[minus_mask]

    alpha = 1.0 / period
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_plus = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * smoothed_plus / smoothed_tr
    minus_di = 100.0 * smoothed_minus / smoothed_tr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return adx, plus_di, minus_di


def add_diagnostics(df: pd.DataFrame, *, entry_ema=True) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    if entry_ema:
        out["ema9"] = close.ewm(span=9, adjust=False).mean()
        out["ema21"] = close.ewm(span=21, adjust=False).mean()
        out["ema9_slope"] = out["ema9"].diff()
        out["ema21_slope"] = out["ema21"].diff()
    out["rsi14"] = _rsi(close, 14)
    out["vwap"] = calc_vwap(out)
    out["atr14"] = calc_atr(out, 14)
    adx, plus_di, minus_di = _adx_bundle(out, 14)
    out["adx14"] = adx
    out["plus_di14"] = plus_di
    out["minus_di14"] = minus_di
    out["avg_volume20"] = pd.to_numeric(out["volume"], errors="coerce").rolling(20).mean()
    out["volume_ratio20"] = pd.to_numeric(out["volume"], errors="coerce") / out[
        "avg_volume20"
    ].replace(0, np.nan)
    return out


def add_15m_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    out = add_diagnostics(df, entry_ema=False)
    if out is None or out.empty:
        return out
    close = pd.to_numeric(out["close"], errors="coerce")
    fast = int(getattr(cfg, "TREND_EMA_FAST", 20))
    slow = int(getattr(cfg, "TREND_EMA_SLOW", 50))
    out["trend_ema_fast"] = close.ewm(span=fast, adjust=False).mean()
    out["trend_ema_slow"] = close.ewm(span=slow, adjust=False).mean()
    return out


def fetch_15m(kite, symbols):
    instruments = {
        x["tradingsymbol"]: x["instrument_token"]
        for x in kite.instruments("NSE")
        if x.get("instrument_type") == "EQ" and x.get("tradingsymbol") in symbols
    }
    result = {}
    for idx, symbol in enumerate(sorted(instruments), 1):
        rows = kite.historical_data(
            instruments[symbol],
            pd.Timestamp("2026-08-01 09:15", tz=IST).to_pydatetime(),
            pd.Timestamp(f"{SESSION_DATE} 15:15", tz=IST).to_pydatetime(),
            "15minute",
        )
        time.sleep(0.35)
        result[symbol] = add_15m_diagnostics(base.prepare_df(rows))
        print(f"[15m {idx:02d}/{len(instruments):02d}] {symbol:<14} rows={len(result[symbol])}")
    return result


def reconstruct_current_policy(kite):
    """Mirror replay_current_policy_20260811.py and return accepted + data."""
    current.configure_date()
    opportunities = base.group_history()
    symbols = sorted({op["symbol"] for op in opportunities})
    print(f"Fetching current-policy source data | opportunities={len(opportunities)} symbols={len(symbols)}")
    minute, three = base.fetch_market_data(kite, symbols)
    enriched = current.enrich(opportunities, three)

    accepted = []
    rejected = []
    sticky_halt_time = None

    for op in enriched:
        now = op["order_time"]
        realized = current.realized_pnl_at(accepted, now)
        if sticky_halt_time is None:
            running = 0.0
            for leg in current.completed_legs(accepted, now):
                running += float(leg["net"])
                if running <= -current.DAILY_LOSS_LIMIT:
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

        per_share_risk = float(op["entry"]) * current.STRATEGY_STOP_PCT / 100.0
        risk_qty = int(current.RISK_AMOUNT / per_share_risk) if per_share_risk > 0 else 0
        qty = min(risk_qty, int(op["actual_qty"]))
        if qty <= 0:
            rejected.append((op, "QTY_ZERO_AT_0_2PCT_RISK"))
            continue

        open_now = current.open_trades_at(accepted, now)
        if any(trade["symbol"] == op["symbol"] for trade in open_now):
            rejected.append((op, "SYMBOL_ALREADY_OPEN"))
            continue

        realized_loss = max(0.0, -realized)
        open_risk = sum(
            current.strategy_risk_for(trade["entry"], current.remaining_qty_at(trade, now))
            for trade in open_now
        )
        proposed_risk = current.strategy_risk_for(op["entry"], qty)
        aggregate = realized_loss + open_risk + proposed_risk
        if aggregate >= current.DAILY_LOSS_LIMIT:
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

        accepted.append(
            {
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
                    "budget": current.DAILY_LOSS_LIMIT,
                },
            }
        )
    return accepted, rejected, minute, three


def latest_completed(df, now, interval_minutes):
    if df is None or df.empty:
        return None
    eligible = df.loc[df["date"] + pd.Timedelta(minutes=interval_minutes) <= now]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def directional_price_vs_ref(direction, price, ref, entry):
    if pd.isna(price) or pd.isna(ref) or entry <= 0:
        return math.nan
    raw = (float(price) - float(ref)) / entry * 100.0
    return raw if direction == "BUY" else -raw


def directional_spread(direction, fast, slow, entry):
    if pd.isna(fast) or pd.isna(slow) or entry <= 0:
        return math.nan
    raw = (float(fast) - float(slow)) / entry * 100.0
    return raw if direction == "BUY" else -raw


def directional_di_edge(direction, plus_di, minus_di):
    if pd.isna(plus_di) or pd.isna(minus_di):
        return math.nan
    edge = float(plus_di) - float(minus_di)
    return edge if direction == "BUY" else -edge


def threshold_time(path: pd.DataFrame, column: str, threshold: float, greater=True):
    if path.empty or column not in path:
        return math.nan
    s = pd.to_numeric(path[column], errors="coerce")
    mask = s >= threshold if greater else s <= threshold
    hits = path.loc[mask]
    if hits.empty:
        return math.nan
    return int(hits.iloc[0]["minute_from_entry"])


def cohen_d(a: pd.Series, b: pd.Series):
    a = pd.to_numeric(a, errors="coerce").dropna()
    b = pd.to_numeric(b, errors="coerce").dropna()
    if len(a) < 2 or len(b) < 2:
        return math.nan
    va, vb = a.var(ddof=1), b.var(ddof=1)
    pooled_num = (len(a) - 1) * va + (len(b) - 1) * vb
    pooled_den = len(a) + len(b) - 2
    if pooled_den <= 0:
        return math.nan
    pooled = math.sqrt(max(0.0, pooled_num / pooled_den))
    if pooled == 0:
        return 0.0
    return (a.mean() - b.mean()) / pooled


def build_paths(accepted, minute, three, fifteen):
    minute_rows = []
    trade_rows = []

    prepared_minute = {s: add_diagnostics(df, entry_ema=True) for s, df in minute.items()}
    prepared_three = {s: add_diagnostics(df, entry_ema=True) for s, df in three.items()}

    for idx, trade in enumerate(accepted, 1):
        symbol = trade["symbol"]
        direction = trade["direction"]
        entry = float(trade["entry"])
        order_time = trade["entry_time"]
        signal_start = trade["signal_start"]
        exit_time = trade["replay"]["exit_time"]
        label = "WIN" if trade["replay"]["net"] > 0 else "LOSS" if trade["replay"]["net"] < 0 else "FLAT"
        trade_id = f"{idx:02d}_{order_time.strftime('%H%M%S')}_{symbol}_{direction}"

        df1 = prepared_minute[symbol]
        df3 = prepared_three[symbol]
        df15 = fifteen.get(symbol)
        rows = df1.loc[
            (df1["date"] >= order_time.floor("min")) & (df1["date"] <= exit_time)
        ].copy()
        if rows.empty:
            continue

        path_so_far = []
        favorable_candles = 0
        adverse_candles = 0
        adverse_streak = 0
        favorable_streak = 0

        for _, row in rows.iterrows():
            now = row["date"]
            minute_no = int(max(0, (now - order_time.floor("min")).total_seconds() // 60))
            path_so_far.append(row)
            since_entry = rows.loc[rows["date"] <= now]
            mfe_entry, mae_entry, current_pct, giveback_entry = base.excursions(
                direction, entry, since_entry, float(row["close"])
            )
            since_signal = df1.loc[
                (df1["date"] >= signal_start.floor("min")) & (df1["date"] <= now)
            ]
            mfe_strategy, mae_strategy, _, giveback_strategy = base.excursions(
                direction, entry, since_signal, float(row["close"])
            )

            sign = 1.0 if direction == "BUY" else -1.0
            body_dir = sign * (float(row["close"]) - float(row["open"])) / entry * 100.0
            if body_dir > 0:
                favorable_candles += 1
                favorable_streak += 1
                adverse_streak = 0
            elif body_dir < 0:
                adverse_candles += 1
                adverse_streak += 1
                favorable_streak = 0
            else:
                adverse_streak = 0
                favorable_streak = 0

            r3 = latest_completed(df3, now, 3)
            r15 = latest_completed(df15, now, 15) if df15 is not None else None

            ema_spread_1 = directional_spread(direction, row.get("ema9"), row.get("ema21"), entry)
            ema9_slope_1 = sign * _finite(row.get("ema9_slope")) / entry * 100.0
            vwap_side_1 = directional_price_vs_ref(
                direction, float(row["close"]), row.get("vwap"), entry
            )
            di_edge_1 = directional_di_edge(
                direction, row.get("plus_di14"), row.get("minus_di14")
            )

            if r3 is not None:
                ema_spread_3 = directional_spread(direction, r3.get("ema9"), r3.get("ema21"), entry)
                vwap_side_3 = directional_price_vs_ref(direction, r3.get("close"), r3.get("vwap"), entry)
                rsi3 = _finite(r3.get("rsi14"))
                adx3 = _finite(r3.get("adx14"))
                vol3 = _finite(r3.get("volume_ratio20"))
            else:
                ema_spread_3 = vwap_side_3 = rsi3 = adx3 = vol3 = math.nan

            if r15 is not None:
                vwap_side_15 = directional_price_vs_ref(direction, r15.get("close"), r15.get("vwap"), entry)
                adx15 = _finite(r15.get("adx14"))
                trend_spread_15 = directional_spread(
                    direction, r15.get("trend_ema_fast"), r15.get("trend_ema_slow"), entry
                )
            else:
                vwap_side_15 = adx15 = trend_spread_15 = math.nan

            minute_rows.append(
                {
                    "trade_id": trade_id,
                    "label": label,
                    "symbol": symbol,
                    "direction": direction,
                    "qty": trade["qty"],
                    "entry_price": entry,
                    "entry_time": order_time,
                    "signal_start": signal_start,
                    "exit_time": exit_time,
                    "minute_time": now,
                    "minute_from_entry": minute_no,
                    "minute_from_signal": (now - signal_start.floor("min")).total_seconds() / 60.0,
                    "entry_adx_stored_15m": trade.get("adx"),
                    "entry_rsi_3m": trade.get("rsi"),
                    "entry_ema9_3m": trade.get("ema9"),
                    "entry_ema21_3m": trade.get("ema21"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "current_pct": current_pct,
                    "mfe_from_entry_pct": mfe_entry,
                    "mae_from_entry_pct": mae_entry,
                    "giveback_from_entry_pct": giveback_entry,
                    "mfe_strategy_clock_pct": mfe_strategy,
                    "mae_strategy_clock_pct": mae_strategy,
                    "giveback_strategy_clock_pct": giveback_strategy,
                    "candle_body_directional_pct": body_dir,
                    "candle_range_pct": (float(row["high"]) - float(row["low"])) / entry * 100.0,
                    "favorable_candle_count": favorable_candles,
                    "adverse_candle_count": adverse_candles,
                    "favorable_candle_streak": favorable_streak,
                    "adverse_candle_streak": adverse_streak,
                    "ema9_1m": row.get("ema9"),
                    "ema21_1m": row.get("ema21"),
                    "ema_spread_directional_1m_pct": ema_spread_1,
                    "ema9_slope_directional_1m_pct": ema9_slope_1,
                    "rsi14_1m": row.get("rsi14"),
                    "vwap_1m": row.get("vwap"),
                    "price_vs_vwap_directional_1m_pct": vwap_side_1,
                    "atr14_1m_pct": _finite(row.get("atr14")) / entry * 100.0,
                    "adx14_1m": row.get("adx14"),
                    "plus_di14_1m": row.get("plus_di14"),
                    "minus_di14_1m": row.get("minus_di14"),
                    "di_edge_directional_1m": di_edge_1,
                    "volume_ratio20_1m": row.get("volume_ratio20"),
                    "ema_spread_directional_3m_pct": ema_spread_3,
                    "price_vs_vwap_directional_3m_pct": vwap_side_3,
                    "rsi14_3m": rsi3,
                    "adx14_3m": adx3,
                    "volume_ratio20_3m": vol3,
                    "adx14_15m": adx15,
                    "trend_ema_spread_directional_15m_pct": trend_spread_15,
                    "price_vs_vwap_directional_15m_pct": vwap_side_15,
                    "final_net": trade["replay"]["net"],
                    "final_gross": trade["replay"]["gross"],
                    "final_costs": trade["replay"]["costs"],
                    "final_mfe_pct": trade["replay"]["mfe"],
                    "final_mae_pct": trade["replay"]["mae"],
                    "exit_reasons": trade["replay"]["reasons"],
                }
            )

        path_df = pd.DataFrame([x for x in minute_rows if x["trade_id"] == trade_id])
        duration = max(0.0, (exit_time - order_time).total_seconds() / 60.0)
        first10 = path_df.loc[path_df["minute_from_entry"] <= 10]
        first5 = path_df.loc[path_df["minute_from_entry"] <= 5]
        trade_rows.append(
            {
                "trade_id": trade_id,
                "label": label,
                "symbol": symbol,
                "direction": direction,
                "qty": trade["qty"],
                "entry_price": entry,
                "entry_time": order_time,
                "exit_time": exit_time,
                "duration_minutes": duration,
                "entry_adx_stored_15m": trade.get("adx"),
                "entry_adx_band": (
                    "20-<30" if trade.get("adx") is not None and trade["adx"] < 30
                    else "30-<40" if trade.get("adx") is not None and trade["adx"] < 40
                    else ">=40"
                ),
                "entry_rsi_3m": trade.get("rsi"),
                "entry_ema_spread_directional_3m_pct": directional_spread(
                    direction, trade.get("ema9"), trade.get("ema21"), entry
                ),
                "net": trade["replay"]["net"],
                "gross": trade["replay"]["gross"],
                "costs": trade["replay"]["costs"],
                "final_mfe_pct": trade["replay"]["mfe"],
                "final_mae_pct": trade["replay"]["mae"],
                "exit_reasons": trade["replay"]["reasons"],
                "time_to_mfe_0_05": threshold_time(path_df, "mfe_from_entry_pct", 0.05, True),
                "time_to_mfe_0_10": threshold_time(path_df, "mfe_from_entry_pct", 0.10, True),
                "time_to_mfe_0_15": threshold_time(path_df, "mfe_from_entry_pct", 0.15, True),
                "time_to_mfe_0_30": threshold_time(path_df, "mfe_from_entry_pct", 0.30, True),
                "time_to_mfe_0_45": threshold_time(path_df, "mfe_from_entry_pct", 0.45, True),
                "time_to_mae_m0_05": threshold_time(path_df, "mae_from_entry_pct", -0.05, False),
                "time_to_mae_m0_10": threshold_time(path_df, "mae_from_entry_pct", -0.10, False),
                "time_to_mae_m0_15": threshold_time(path_df, "mae_from_entry_pct", -0.15, False),
                "time_to_mae_m0_30": threshold_time(path_df, "mae_from_entry_pct", -0.30, False),
                "time_to_mae_m0_45": threshold_time(path_df, "mae_from_entry_pct", -0.45, False),
                "first5_avg_current_pct": first5["current_pct"].mean() if not first5.empty else math.nan,
                "first10_avg_current_pct": first10["current_pct"].mean() if not first10.empty else math.nan,
                "first5_positive_fraction": (first5["current_pct"] > 0).mean() if not first5.empty else math.nan,
                "first10_positive_fraction": (first10["current_pct"] > 0).mean() if not first10.empty else math.nan,
                "first10_max_mfe_pct": first10["mfe_from_entry_pct"].max() if not first10.empty else math.nan,
                "first10_min_mae_pct": first10["mae_from_entry_pct"].min() if not first10.empty else math.nan,
                "first10_adverse_candle_fraction": (
                    (first10["candle_body_directional_pct"] < 0).mean() if not first10.empty else math.nan
                ),
            }
        )

    return pd.DataFrame(trade_rows), pd.DataFrame(minute_rows)


def checkpoint_comparison(paths: pd.DataFrame):
    rows = []
    for checkpoint in CHECKPOINTS:
        snap_parts = []
        for _, group in paths.groupby("trade_id"):
            eligible = group.loc[group["minute_from_entry"] <= checkpoint]
            if eligible.empty:
                continue
            # Only call it an observed checkpoint if the trade was still open
            # through this minute.  This avoids carrying the last value forward
            # after an exit and makes the sample-size reduction explicit.
            if group["minute_from_entry"].max() < checkpoint:
                continue
            snap_parts.append(eligible.iloc[-1])
        if not snap_parts:
            continue
        snap = pd.DataFrame(snap_parts)
        for feature in COMPARE_FEATURES:
            if feature not in snap:
                continue
            win = pd.to_numeric(snap.loc[snap["label"] == "WIN", feature], errors="coerce").dropna()
            loss = pd.to_numeric(snap.loc[snap["label"] == "LOSS", feature], errors="coerce").dropna()
            rows.append(
                {
                    "checkpoint_minute": checkpoint,
                    "feature": feature,
                    "winner_n": len(win),
                    "loser_n": len(loss),
                    "winner_mean": win.mean() if len(win) else math.nan,
                    "winner_median": win.median() if len(win) else math.nan,
                    "loser_mean": loss.mean() if len(loss) else math.nan,
                    "loser_median": loss.median() if len(loss) else math.nan,
                    "winner_minus_loser_mean": (
                        win.mean() - loss.mean() if len(win) and len(loss) else math.nan
                    ),
                    "cohen_d_winner_minus_loser": cohen_d(win, loss),
                }
            )
    return pd.DataFrame(rows)


def threshold_diagnostics(paths: pd.DataFrame):
    """Evaluate simple early-failure observations without changing strategy."""
    rules = [
        ("current<0", lambda r: r["current_pct"] < 0),
        ("mfe<0.05", lambda r: r["mfe_from_entry_pct"] < 0.05),
        ("mfe<0.10", lambda r: r["mfe_from_entry_pct"] < 0.10),
        ("mfe<0.15", lambda r: r["mfe_from_entry_pct"] < 0.15),
        ("mae<=-0.10", lambda r: r["mae_from_entry_pct"] <= -0.10),
        ("mae<=-0.15", lambda r: r["mae_from_entry_pct"] <= -0.15),
        ("ema1m_against_trade", lambda r: r["ema_spread_directional_1m_pct"] < 0),
        ("vwap1m_against_trade", lambda r: r["price_vs_vwap_directional_1m_pct"] < 0),
        ("di1m_against_trade", lambda r: r["di_edge_directional_1m"] < 0),
        (
            "mfe<0.15 AND current<0",
            lambda r: (r["mfe_from_entry_pct"] < 0.15) and (r["current_pct"] < 0),
        ),
        (
            "mfe<0.15 AND current<0 AND ema1m_against",
            lambda r: (r["mfe_from_entry_pct"] < 0.15)
            and (r["current_pct"] < 0)
            and (r["ema_spread_directional_1m_pct"] < 0),
        ),
    ]
    out = []
    for checkpoint in [3, 5, 8, 10, 12, 15, 20]:
        snap_rows = []
        for _, group in paths.groupby("trade_id"):
            if group["minute_from_entry"].max() < checkpoint:
                continue
            eligible = group.loc[group["minute_from_entry"] <= checkpoint]
            if not eligible.empty:
                snap_rows.append(eligible.iloc[-1])
        if not snap_rows:
            continue
        snap = pd.DataFrame(snap_rows)
        for name, fn in rules:
            flags = snap.apply(fn, axis=1)
            total_w = int((snap["label"] == "WIN").sum())
            total_l = int((snap["label"] == "LOSS").sum())
            flagged_w = int((flags & (snap["label"] == "WIN")).sum())
            flagged_l = int((flags & (snap["label"] == "LOSS")).sum())
            out.append(
                {
                    "checkpoint_minute": checkpoint,
                    "rule": name,
                    "winner_sample": total_w,
                    "loser_sample": total_l,
                    "winners_flagged": flagged_w,
                    "losers_flagged": flagged_l,
                    "winner_flag_rate_pct": 100.0 * flagged_w / total_w if total_w else math.nan,
                    "loser_flag_rate_pct": 100.0 * flagged_l / total_l if total_l else math.nan,
                    "precision_if_used_as_loss_flag_pct": (
                        100.0 * flagged_l / (flagged_l + flagged_w)
                        if flagged_l + flagged_w
                        else math.nan
                    ),
                }
            )
    return pd.DataFrame(out)


def print_research_summary(trades, paths, comparison, thresholds):
    print("\n" + "=" * 128)
    print("AUG-11 WINNER vs LOSER MINUTE-PATH RESEARCH — CURRENT PAPER POLICY")
    print("=" * 128)
    print(f"Trades studied: {len(trades)} | winners={(trades.label == 'WIN').sum()} | losers={(trades.label == 'LOSS').sum()}")
    print(f"Winner net: Rs {trades.loc[trades.label == 'WIN', 'net'].sum():.2f}")
    print(f"Loser net : Rs {trades.loc[trades.label == 'LOSS', 'net'].sum():.2f}")

    print("\nENTRY + FINAL PATH COMPARISON")
    for feature in [
        "entry_adx_stored_15m",
        "entry_rsi_3m",
        "entry_ema_spread_directional_3m_pct",
        "duration_minutes",
        "final_mfe_pct",
        "final_mae_pct",
        "first5_avg_current_pct",
        "first10_avg_current_pct",
        "first10_max_mfe_pct",
        "first10_min_mae_pct",
        "first10_adverse_candle_fraction",
    ]:
        w = pd.to_numeric(trades.loc[trades.label == "WIN", feature], errors="coerce").dropna()
        l = pd.to_numeric(trades.loc[trades.label == "LOSS", feature], errors="coerce").dropna()
        print(
            f"  {feature:<42} W mean={w.mean():>9.4f} med={w.median():>9.4f} n={len(w):<2} | "
            f"L mean={l.mean():>9.4f} med={l.median():>9.4f} n={len(l):<2} | d={cohen_d(w,l):>7.3f}"
        )

    print("\nMINUTE CHECKPOINT CORE METRICS")
    core = [
        "current_pct",
        "mfe_from_entry_pct",
        "mae_from_entry_pct",
        "ema_spread_directional_1m_pct",
        "price_vs_vwap_directional_1m_pct",
        "rsi14_1m",
        "adx14_1m",
        "volume_ratio20_1m",
    ]
    for cp in [1, 3, 5, 8, 10, 15, 20]:
        sub = comparison.loc[
            (comparison["checkpoint_minute"] == cp) & comparison["feature"].isin(core)
        ]
        if sub.empty:
            continue
        print(f"\n  +{cp} MINUTES")
        for _, r in sub.iterrows():
            print(
                f"    {r['feature']:<37} W={r['winner_mean']:>9.4f} (n={int(r['winner_n'])}) "
                f"L={r['loser_mean']:>9.4f} (n={int(r['loser_n'])}) "
                f"diff={r['winner_minus_loser_mean']:>9.4f} d={r['cohen_d_winner_minus_loser']:>7.3f}"
            )

    print("\nTOP EARLY LOSS FLAGS BY SEPARATION")
    t = thresholds.copy()
    t["separation"] = t["loser_flag_rate_pct"] - t["winner_flag_rate_pct"]
    t = t.sort_values(["separation", "precision_if_used_as_loss_flag_pct"], ascending=False).head(20)
    for _, r in t.iterrows():
        print(
            f"  +{int(r['checkpoint_minute']):>2}m {r['rule']:<43} "
            f"loss_flag={r['loser_flag_rate_pct']:>6.1f}% win_flag={r['winner_flag_rate_pct']:>6.1f}% "
            f"sep={r['separation']:>6.1f}pp precision={r['precision_if_used_as_loss_flag_pct']:>6.1f}%"
        )

    print("\nTIME-TO-EXCURSION COMPARISON (minutes; NaN = threshold never reached)")
    for feature in [
        "time_to_mfe_0_10",
        "time_to_mfe_0_15",
        "time_to_mfe_0_30",
        "time_to_mae_m0_10",
        "time_to_mae_m0_15",
        "time_to_mae_m0_30",
    ]:
        w = pd.to_numeric(trades.loc[trades.label == "WIN", feature], errors="coerce")
        l = pd.to_numeric(trades.loc[trades.label == "LOSS", feature], errors="coerce")
        print(
            f"  {feature:<25} W reached={w.notna().sum():>2}/10 median={w.median():>5.1f} | "
            f"L reached={l.notna().sum():>2}/10 median={l.median():>5.1f}"
        )

    print("\nFILES")
    print(f"  {OUT_TRADE}")
    print(f"  {OUT_MINUTE}")
    print(f"  {OUT_COMPARE}")
    print(f"  {OUT_THRESHOLDS}")
    print("\nIMPORTANT: 1m/3m diagnostic ADX is retrospective research only; entry authorization used stored stock 15m ADX.")


def main():
    print("Connecting to Kite...")
    kite = base.get_kite_client()
    accepted, rejected, minute, three = reconstruct_current_policy(kite)
    if not accepted:
        raise SystemExit("No current-policy trades reconstructed")

    accepted_symbols = sorted({t["symbol"] for t in accepted})
    print(f"\nAccepted current-policy trades={len(accepted)} | accepted symbols={len(accepted_symbols)}")
    fifteen = fetch_15m(kite, accepted_symbols)

    trades, paths = build_paths(accepted, minute, three, fifteen)
    comparison = checkpoint_comparison(paths)
    thresholds = threshold_diagnostics(paths)

    trades.to_csv(OUT_TRADE, index=False)
    paths.to_csv(OUT_MINUTE, index=False)
    comparison.to_csv(OUT_COMPARE, index=False)
    thresholds.to_csv(OUT_THRESHOLDS, index=False)

    print_research_summary(trades, paths, comparison, thresholds)


if __name__ == "__main__":
    main()
