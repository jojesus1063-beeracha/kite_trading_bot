#!/usr/bin/env python3
"""Build a liquid, breakout-ready PAPER watchlist from NSE movers."""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auth import get_kite_client
from auto_watchlist import SelectorSettings, atomic_write_json, evaluate_quote
from indicators import adx, atr, ema, vwap
from paper_full_universe_top60_selector import (
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    fetch_selector_quotes,
    ordinary_equity_rejection_reason,
    write_paper_watchlist,
)


IST = ZoneInfo("Asia/Kolkata")
STRATEGY_NAME = "NSE_BREAKOUT_READY_TOP25_GAINERS_TOP15_LOSERS"
DEFAULT_WINNERS = 25
DEFAULT_LOSERS = 15
DEFAULT_MIN_ABS_CHANGE_PCT = 1.50
DEFAULT_MAX_ABS_CHANGE_PCT = 8.00
DEFAULT_MIN_DAY_RANGE_PCT = 0.75
DEFAULT_NEAR_BREAKOUT_PCT = 0.50
DEFAULT_MIN_RVOL = 1.20
DEFAULT_MIN_ATR_PCT = 0.30
DEFAULT_MAX_ATR_PCT = 1.50
DEFAULT_MAX_VWAP_DISTANCE_PCT = 1.00
DEFAULT_BUY_MIN_ADX = 25.0
DEFAULT_SELL_MIN_ADX = 20.0
DEFAULT_HISTORY_MULTIPLIER = 4
HISTORY_REQUEST_DELAY_SECONDS = 0.36


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an NSE top-gainers/top-losers PAPER watchlist."
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--winners", type=int, default=DEFAULT_WINNERS)
    parser.add_argument("--losers", type=int, default=DEFAULT_LOSERS)
    parser.add_argument("--min-price", type=float, default=50.0)
    parser.add_argument("--max-price", type=float, default=3000.0)
    parser.add_argument("--min-turnover", type=float, default=100_000_000.0)
    parser.add_argument("--max-spread-pct", type=float, default=0.15)
    parser.add_argument("--min-circuit-distance-pct", type=float, default=1.0)
    parser.add_argument("--min-abs-change-pct", type=float, default=DEFAULT_MIN_ABS_CHANGE_PCT)
    parser.add_argument("--max-abs-change-pct", type=float, default=DEFAULT_MAX_ABS_CHANGE_PCT)
    parser.add_argument("--min-day-range-pct", type=float, default=DEFAULT_MIN_DAY_RANGE_PCT)
    parser.add_argument("--near-breakout-pct", type=float, default=DEFAULT_NEAR_BREAKOUT_PCT)
    parser.add_argument("--min-rvol", type=float, default=DEFAULT_MIN_RVOL)
    parser.add_argument("--min-atr-pct", type=float, default=DEFAULT_MIN_ATR_PCT)
    parser.add_argument("--max-atr-pct", type=float, default=DEFAULT_MAX_ATR_PCT)
    parser.add_argument("--max-vwap-distance-pct", type=float, default=DEFAULT_MAX_VWAP_DISTANCE_PCT)
    parser.add_argument("--buy-min-adx", type=float, default=DEFAULT_BUY_MIN_ADX)
    parser.add_argument("--sell-min-adx", type=float, default=DEFAULT_SELL_MIN_ADX)
    parser.add_argument("--history-multiplier", type=int, default=DEFAULT_HISTORY_MULTIPLIER)
    parser.add_argument("--allow-missing-depth", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def movement_rejection_reason(
    candidate: dict[str, Any],
    *,
    min_abs_change_pct: float,
    max_abs_change_pct: float,
    min_day_range_pct: float,
) -> str | None:
    change = abs(float(candidate.get("change_pct") or 0.0))
    day_range = float(candidate.get("day_range_pct") or 0.0)
    if change > max_abs_change_pct:
        return "absolute_change_above_maximum"
    return None


def session_fraction(now: datetime) -> float:
    """Elapsed fraction of the 09:15-15:30 NSE cash session."""
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    session_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now <= session_start:
        return 0.01
    if now >= session_end:
        return 1.0
    return max(0.01, (now - session_start).total_seconds() / (session_end - session_start).total_seconds())


def completed_context_metrics(
    candles: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    now: datetime,
    near_breakout_pct: float,
    min_rvol: float,
    min_atr_pct: float,
    max_atr_pct: float,
    max_vwap_distance_pct: float,
    buy_min_adx: float,
    sell_min_adx: float,
) -> dict[str, Any]:
    """Compute point-in-time ranking telemetry from completed 3-minute bars."""
    import pandas as pd

    direction = "BUY" if float(candidate.get("change_pct") or 0.0) > 0 else "SELL"
    frame = pd.DataFrame(candles)
    unavailable = lambda reason: {
        "context_available": False,
        "context_reason": reason,
        "direction": direction,
        "primary_breakout_ready": False,
        "watchlist_score": 0.0,
    }
    if frame.empty or len(frame) < 30:
        return unavailable("INSUFFICIENT_3M_HISTORY")
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return unavailable("INVALID_3M_COLUMNS")

    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is None:
        frame["date"] = frame["date"].dt.tz_localize(IST)
    else:
        frame["date"] = frame["date"].dt.tz_convert(IST)
    cutoff = pd.Timestamp(now).floor("3min")
    frame = frame.loc[frame["date"] < cutoff].sort_values("date").reset_index(drop=True)
    numeric = ["open", "high", "low", "close", "volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=numeric)
    if len(frame) < 30:
        return unavailable("INSUFFICIENT_COMPLETED_3M_HISTORY")

    last_price = float(candidate.get("last_price") or 0.0)
    prior = frame.tail(20)
    n20_high = float(prior["high"].max())
    n20_low = float(prior["low"].min())
    level = n20_high if direction == "BUY" else n20_low
    signed_gap = (
        ((level - last_price) / level) * 100.0
        if direction == "BUY"
        else ((last_price - level) / level) * 100.0
    )
    near_breakout = abs(signed_gap) <= near_breakout_pct

    ema9 = float(ema(frame, 9).iloc[-1])
    ema21 = float(ema(frame, 21).iloc[-1])
    ema_aligned = ema9 > ema21 if direction == "BUY" else ema9 < ema21
    adx_value = float(adx(frame, 14).iloc[-1])
    adx_minimum = buy_min_adx if direction == "BUY" else sell_min_adx
    adx_aligned = math.isfinite(adx_value) and adx_value >= adx_minimum

    atr_value = float(atr(frame, 14).iloc[-1])
    atr_pct = (atr_value / last_price) * 100.0 if last_price > 0 else math.nan
    atr_usable = math.isfinite(atr_pct) and min_atr_pct <= atr_pct <= max_atr_pct

    prior_volume_sma = float(frame["volume"].iloc[-21:-1].mean())
    latest_volume = float(frame["volume"].iloc[-1])
    rvol = latest_volume / prior_volume_sma if prior_volume_sma > 0 else math.nan
    rvol_aligned = math.isfinite(rvol) and rvol >= min_rvol

    session = frame.loc[frame["date"].dt.date == pd.Timestamp(now).date()].copy()
    session_vwap = float(vwap(session).iloc[-1]) if not session.empty else math.nan
    vwap_distance = (
        abs(last_price - session_vwap) / session_vwap * 100.0
        if math.isfinite(session_vwap) and session_vwap > 0
        else math.nan
    )
    vwap_usable = math.isfinite(vwap_distance) and vwap_distance <= max_vwap_distance_pct

    primary = all((near_breakout, ema_aligned, adx_aligned, atr_usable, rvol_aligned, vwap_usable))
    proximity_score = max(0.0, 1.0 - abs(signed_gap) / max(near_breakout_pct, 1e-9)) * 30.0
    turnover_score = min(math.log10(max(float(candidate.get("turnover") or 1.0), 1.0)) / 10.0, 1.0) * 25.0
    rvol_score = min(rvol / 3.0, 1.0) * 20.0 if math.isfinite(rvol) else 0.0
    adx_score = min(adx_value / 50.0, 1.0) * 15.0 if math.isfinite(adx_value) else 0.0
    spread = float(candidate.get("spread_pct") or 0.15)
    spread_score = max(0.0, 1.0 - spread / 0.15) * 5.0
    vwap_score = (
        max(0.0, 1.0 - vwap_distance / max(max_vwap_distance_pct, 1e-9)) * 5.0
        if math.isfinite(vwap_distance)
        else 0.0
    )

    return {
        "context_available": True,
        "direction": direction,
        "n20_high": round(n20_high, 4),
        "n20_low": round(n20_low, 4),
        "n20_signed_gap_pct": round(signed_gap, 4),
        "near_n20_breakout": near_breakout,
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "ema_direction_aligned": ema_aligned,
        "adx": round(adx_value, 4),
        "adx_minimum": adx_minimum,
        "adx_aligned": adx_aligned,
        "atr_pct": round(atr_pct, 4),
        "atr_usable": atr_usable,
        "latest_3m_rvol": round(rvol, 4),
        "rvol_aligned": rvol_aligned,
        "session_vwap": None if not math.isfinite(session_vwap) else round(session_vwap, 4),
        "vwap_distance_pct": None if not math.isfinite(vwap_distance) else round(vwap_distance, 4),
        "vwap_usable": vwap_usable,
        "primary_breakout_ready": primary,
        "watchlist_score": round(
            proximity_score + turnover_score + rvol_score + adx_score + spread_score + vwap_score,
            6,
        ),
    }


def enrich_intraday_context(
    kite: Any,
    candidates: list[dict[str, Any]],
    *,
    now: datetime,
    args: argparse.Namespace,
) -> Counter[str]:
    failures: Counter[str] = Counter()
    for index, candidate in enumerate(candidates):
        try:
            candles = kite.historical_data(
                int(candidate["instrument_token"]),
                now - timedelta(days=7),
                now,
                "3minute",
            )
            candidate.update(completed_context_metrics(
                candles,
                candidate,
                now=now,
                near_breakout_pct=args.near_breakout_pct,
                min_rvol=args.min_rvol,
                min_atr_pct=args.min_atr_pct,
                max_atr_pct=args.max_atr_pct,
                max_vwap_distance_pct=args.max_vwap_distance_pct,
                buy_min_adx=args.buy_min_adx,
                sell_min_adx=args.sell_min_adx,
            ))
        except Exception as exc:
            failures[type(exc).__name__] += 1
            candidate.update({
                "context_available": False,
                "context_reason": f"FETCH_FAILED:{type(exc).__name__}",
                "primary_breakout_ready": False,
                "watchlist_score": 0.0,
            })
        if index + 1 < len(candidates):
            time.sleep(HISTORY_REQUEST_DELAY_SECONDS)
    return failures


def cleaned_nse_equities(kite: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    instruments = kite.instruments("NSE")
    if not isinstance(instruments, list):
        raise RuntimeError("Kite NSE instrument response was not a list")

    rows: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    seen: set[str] = set()
    for instrument in instruments:
        reason = ordinary_equity_rejection_reason(instrument, "NSE")
        if reason is not None:
            rejections[reason] += 1
            continue
        symbol = str(instrument.get("tradingsymbol") or "").strip().upper()
        if symbol in seen:
            rejections["duplicate_symbol"] += 1
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "exchange": "NSE",
            "company_name": str(instrument.get("name") or "").strip(),
            "industry": "",
            "instrument_token": instrument.get("instrument_token"),
            "tick_size": instrument.get("tick_size", 0.05),
        })
    return rows, {
        "raw_nse_instruments": len(instruments),
        "clean_nse_equities": len(rows),
        "cleaning_rejections": dict(sorted(rejections.items())),
    }


def select_top_movers(
    candidates: list[dict[str, Any]],
    winners: int = DEFAULT_WINNERS,
    losers: int = DEFAULT_LOSERS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Prioritise breakout-ready movers, then safely backfill by score."""
    def rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
        gap = item.get("n20_signed_gap_pct")
        return (
            not bool(item.get("primary_breakout_ready")),
            not bool(item.get("movement_ready")),
            -float(item.get("watchlist_score") or 0.0),
            abs(float(gap)) if gap is not None else 999.0,
            -abs(float(item.get("change_pct") or 0.0)),
            -float(item.get("turnover") or 0.0),
            str(item.get("symbol") or ""),
        )

    gainers = sorted(
        (item for item in candidates if float(item.get("change_pct") or 0.0) > 0),
        key=rank_key,
    )[:winners]
    decliners = sorted(
        (item for item in candidates if float(item.get("change_pct") or 0.0) < 0),
        key=rank_key,
    )[:losers]

    for rank, item in enumerate(gainers, 1):
        item["mover_group"] = "GAINER"
        item["mover_rank"] = rank
        item["selection_tier"] = (
            "PRIMARY_BREAKOUT_READY"
            if item.get("primary_breakout_ready")
            else "SAFE_MOVER_BACKFILL"
        )
    for rank, item in enumerate(decliners, 1):
        item["mover_group"] = "LOSER"
        item["mover_rank"] = rank
        item["selection_tier"] = (
            "PRIMARY_BREAKOUT_READY"
            if item.get("primary_breakout_ready")
            else "SAFE_MOVER_BACKFILL"
        )
    return gainers + decliners, gainers, decliners


def main() -> int:
    args = parse_args()
    if args.winners <= 0 or args.losers <= 0:
        raise SystemExit("ERROR: --winners and --losers must be positive")
    if not 0 < args.min_abs_change_pct <= args.max_abs_change_pct:
        raise SystemExit("ERROR: invalid absolute-change range")
    if args.history_multiplier < 1:
        raise SystemExit("ERROR: --history-multiplier must be >= 1")

    generated_now = datetime.now(IST)
    elapsed_fraction = session_fraction(generated_now)
    effective_current_turnover = args.min_turnover * elapsed_fraction
    settings = SelectorSettings(
        top_n=args.winners + args.losers,
        min_selected=args.winners + args.losers,
        min_price=args.min_price,
        max_price=args.max_price,
        min_turnover=effective_current_turnover,
        max_spread_pct=args.max_spread_pct,
        min_circuit_distance_pct=args.min_circuit_distance_pct,
        enable_open_equals_low_priority=False,
        min_live_momentum_pct=0.0,
        enable_previous_day_momentum_fallback=False,
    )

    print("Connecting to Kite for NSE top-movers PAPER selector...")
    kite = get_kite_client()
    rows, cleaning = cleaned_nse_equities(kite)
    quote_keys = [f"NSE:{row['symbol']}" for row in rows]
    quotes = fetch_selector_quotes(kite, quote_keys)
    minimum_quotes = math.ceil(len(rows) * 0.90)
    if len(quotes) < minimum_quotes:
        raise SystemExit(
            f"FAIL: incomplete quote coverage {len(quotes)}/{len(rows)}; "
            f"minimum {minimum_quotes}"
        )

    eligible: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    for row in rows:
        quote = quotes.get(f"NSE:{row['symbol']}")
        if not isinstance(quote, dict):
            rejections["missing_quote"] += 1
            continue
        candidate, reason = evaluate_quote(
            row,
            quote,
            settings,
            allow_missing_depth=args.allow_missing_depth,
        )
        if candidate is None:
            rejections[reason or "unknown"] += 1
            continue
        candidate.update({
            "exchange": "NSE",
            "instrument_token": row["instrument_token"],
            "ordinary_equity_clean": True,
            "projected_session_turnover": round(
                float(candidate.get("turnover") or 0.0) / elapsed_fraction,
                2,
            ),
        })
        movement_reason = movement_rejection_reason(
            candidate,
            min_abs_change_pct=args.min_abs_change_pct,
            max_abs_change_pct=args.max_abs_change_pct,
            min_day_range_pct=args.min_day_range_pct,
        )
        if movement_reason is not None:
            rejections[movement_reason] += 1
            continue
        candidate["movement_ready"] = (
            abs(float(candidate.get("change_pct") or 0.0)) >= args.min_abs_change_pct
            and float(candidate.get("day_range_pct") or 0.0) >= args.min_day_range_pct
        )
        eligible.append(candidate)

    gainers_for_context = sorted(
        (item for item in eligible if float(item.get("change_pct") or 0.0) > 0),
        key=lambda item: (
            -float(item.get("change_pct") or 0.0),
            -float(item.get("turnover") or 0.0),
        ),
    )[: args.winners * args.history_multiplier]
    losers_for_context = sorted(
        (item for item in eligible if float(item.get("change_pct") or 0.0) < 0),
        key=lambda item: (
            float(item.get("change_pct") or 0.0),
            -float(item.get("turnover") or 0.0),
        ),
    )[: args.losers * args.history_multiplier]
    context_pool = gainers_for_context + losers_for_context
    context_failures = enrich_intraday_context(
        kite,
        context_pool,
        now=generated_now,
        args=args,
    )
    for candidate in context_pool:
        candidate["primary_breakout_ready"] = bool(
            candidate.get("primary_breakout_ready")
            and candidate.get("movement_ready")
        )

    selected, gainers, losers = select_top_movers(
        eligible, args.winners, args.losers
    )
    if len(gainers) != args.winners or len(losers) != args.losers:
        raise SystemExit(
            f"FAIL: require {args.winners} gainers and {args.losers} losers; "
            f"received {len(gainers)} and {len(losers)}"
        )

    generated_at = generated_now.isoformat(timespec="seconds")
    config_backup = None
    if args.write:
        config_backup = write_paper_watchlist(
            args.config,
            selected,
            min_selected=args.winners + args.losers,
            runtime_dir=args.report.parent,
        )

    report = {
        "status": "success",
        "paper_only": True,
        "strategy": STRATEGY_NAME,
        "mode": "WRITE_CONFIG" if args.write else "READ_ONLY",
        "generated_at": generated_at,
        "universe_source": "KITE_CLEAN_ORDINARY_NSE_EQUITIES",
        "selection_definition": "percentage change from previous close",
        "requested_gainers": args.winners,
        "requested_losers": args.losers,
        "cleaning": cleaning,
        "quotes_received": len(quotes),
        "eligible_after_safety_filters": len(eligible),
        "strict_rejections": dict(sorted(rejections.items())),
        "context_pool_size": len(context_pool),
        "context_fetch_failures": dict(sorted(context_failures.items())),
        "primary_breakout_ready_selected": sum(
            bool(item.get("primary_breakout_ready")) for item in selected
        ),
        "safety_filters": {
            "min_price": args.min_price,
            "max_price": args.max_price,
            "min_turnover": args.min_turnover,
            "effective_current_turnover": round(effective_current_turnover, 2),
            "session_fraction": round(elapsed_fraction, 6),
            "max_spread_pct": args.max_spread_pct,
            "min_circuit_distance_pct": args.min_circuit_distance_pct,
            "min_abs_change_pct": args.min_abs_change_pct,
            "max_abs_change_pct": args.max_abs_change_pct,
            "min_day_range_pct": args.min_day_range_pct,
        },
        "ranking_context": {
            "near_breakout_pct": args.near_breakout_pct,
            "minimum_latest_3m_rvol": args.min_rvol,
            "atr_pct_range": [args.min_atr_pct, args.max_atr_pct],
            "maximum_vwap_distance_pct": args.max_vwap_distance_pct,
            "buy_minimum_adx": args.buy_min_adx,
            "sell_minimum_adx": args.sell_min_adx,
            "ema_direction": "EMA9_ABOVE_EMA21_BUY_AND_BELOW_SELL",
            "backfill_policy": "SAFE_MOVERS_FILL_ANY_PRIMARY_SHORTFALL",
        },
        "selected_count": len(selected),
        "selected_gainers": gainers,
        "selected_losers": losers,
        "selected": selected,
        "config_backup": None if config_backup is None else str(config_backup),
    }
    payload = {
        "status": "success",
        "paper_only": True,
        "strategy": STRATEGY_NAME,
        "generated_at": generated_at,
        "watchlist": [
            {"symbol": item["symbol"], "exchange": "NSE"}
            for item in selected
        ],
        "selected_details": selected,
    }
    atomic_write_json(args.report, report)
    atomic_write_json(args.output, payload)

    print("\n===== NSE BREAKOUT-READY TOP 25 GAINERS + TOP 15 LOSERS =====")
    for item in selected:
        print(
            f"{item['mover_group']:<6} #{item['mover_rank']:02d} "
            f"NSE:{item['symbol']:<20} change={item['change_pct']:+.2f}% "
            f"turnover={item['turnover']:,.0f} tier={item['selection_tier']} "
            f"n20_gap={item.get('n20_signed_gap_pct')} "
            f"score={item.get('watchlist_score')}"
        )
    print("Watchlist count:", len(selected))
    print("Config backup:", config_backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
