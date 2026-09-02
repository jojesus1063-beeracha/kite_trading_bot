#!/usr/bin/env python3
"""Read-only full Zerodha equity-universe top-60 momentum selector.

Scans regular NSE+BSE EQ instruments from Kite rather than the NIFTY 500,
then applies the proposed tighter morning filter and ranking model.

Safe by default: writes only runtime research JSON; never modifies
user_config.json and never starts/stops a trading service.
"""
from __future__ import annotations

import argparse
import math
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auth import get_kite_client
from auto_watchlist import (
    SelectorSettings,
    atomic_write_json,
    best_bid_ask,
    calculate_previous_day_momentum,
    evaluate_quote,
    fetch_full_quotes,
    positive_float,
    positive_int,
)
from scan_all_zerodha_equities import equity_instruments

IST = ZoneInfo("Asia/Kolkata")
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "runtime" / "zerodha_universe" / "top60_momentum_watchlist.json"
DEFAULT_REPORT = PROJECT_DIR / "runtime" / "zerodha_universe" / "top60_momentum_report.json"

TOP_N = 60
MIN_PRICE = 20.0
MAX_PRICE = 2200.0
MIN_TURNOVER = 1_000_000.0
MAX_SPREAD_PCT = 0.25
MIN_CIRCUIT_DISTANCE_PCT = 1.0
MIN_ABS_CHANGE_PCT = 0.30
MIN_DAY_RANGE_PCT = 0.40
OPEN_EXTREME_TOLERANCE_TICKS = 0
PREVIOUS_DAY_BONUS_MAX = 5.0
HISTORY_LOOKBACK_DAYS = 45
HISTORY_DELAY_SECONDS = 0.36
HISTORY_RECHECK_MULTIPLIER = 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scan all Zerodha NSE/BSE equities and rank a top-60 morning watchlist.")
    p.add_argument("--top", type=int, default=TOP_N)
    p.add_argument("--min-selected", type=int, default=TOP_N)
    p.add_argument("--max-price", type=float, default=MAX_PRICE)
    p.add_argument("--min-turnover", type=float, default=MIN_TURNOVER)
    p.add_argument("--max-spread-pct", type=float, default=MAX_SPREAD_PCT)
    p.add_argument("--min-circuit-distance-pct", type=float, default=MIN_CIRCUIT_DISTANCE_PCT)
    p.add_argument("--min-abs-change-pct", type=float, default=MIN_ABS_CHANGE_PCT)
    p.add_argument("--min-day-range-pct", type=float, default=MIN_DAY_RANGE_PCT)
    p.add_argument("--history-candidates", type=int, default=TOP_N * HISTORY_RECHECK_MULTIPLIER)
    p.add_argument("--allow-missing-depth", action="store_true")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return p.parse_args()


def movement_gate(change_pct: float, day_range_pct: float, min_change: float, min_range: float) -> bool:
    return abs(change_pct) >= min_change or day_range_pct >= min_range


def within_ticks(a: float, b: float, tick_size: float, tolerance_ticks: int) -> bool:
    tolerance = max(tolerance_ticks, 0) * max(tick_size, 0.0) + 1e-9
    return abs(a - b) <= tolerance


def directional_open_extreme_bonus(
    change_pct: float,
    open_price: float,
    high_price: float,
    low_price: float,
    tick_size: float,
    tolerance_ticks: int = OPEN_EXTREME_TOLERANCE_TICKS,
) -> tuple[float, str]:
    if change_pct > 0 and within_ticks(open_price, low_price, tick_size, tolerance_ticks):
        return 5.0, "BULLISH_OPEN_EQUALS_LOW"
    if change_pct < 0 and within_ticks(open_price, high_price, tick_size, tolerance_ticks):
        return 5.0, "BEARISH_OPEN_EQUALS_HIGH"
    return 0.0, "NONE"


def live_score_components(candidate: dict[str, Any], quote: dict[str, Any]) -> dict[str, float | str]:
    turnover = positive_float(candidate.get("turnover"))
    volume = positive_int(candidate.get("volume"))
    change_pct = positive_float(candidate.get("change_pct"))
    day_range_pct = positive_float(candidate.get("day_range_pct"))
    spread_pct = candidate.get("spread_pct")
    spread = positive_float(spread_pct, MAX_SPREAD_PCT) if spread_pct is not None else MAX_SPREAD_PCT
    last_price = positive_float(candidate.get("last_price"))
    tick_size = positive_float(candidate.get("tick_size"), 0.05)

    ohlc = quote.get("ohlc") or {}
    open_price = positive_float(ohlc.get("open"))
    high_price = positive_float(ohlc.get("high"))
    low_price = positive_float(ohlc.get("low"))

    _, _, bid_qty, ask_qty = best_bid_ask(quote)
    visible_depth_value = (bid_qty + ask_qty) * last_price

    turnover_points = min(math.log10(max(turnover, 1.0)) / 9.0, 1.0) * 30.0
    volume_points = min(math.log10(max(volume, 1)) / 7.0, 1.0) * 20.0
    movement_strength = max(abs(change_pct), day_range_pct)
    movement_points = min(movement_strength / 3.0, 1.0) * 25.0
    spread_points = max(0.0, 1.0 - (spread / MAX_SPREAD_PCT)) * 10.0
    depth_points = min(math.log10(max(visible_depth_value, 1.0)) / 8.0, 1.0) * 5.0
    open_extreme_points, open_extreme_reason = directional_open_extreme_bonus(
        change_pct, open_price, high_price, low_price, tick_size
    )

    subtotal = turnover_points + volume_points + movement_points + spread_points + depth_points + open_extreme_points
    return {
        "turnover_points": round(turnover_points, 6),
        "volume_points": round(volume_points, 6),
        "movement_points": round(movement_points, 6),
        "spread_points": round(spread_points, 6),
        "depth_points": round(depth_points, 6),
        "open_extreme_points": round(open_extreme_points, 6),
        "open_extreme_reason": open_extreme_reason,
        "live_subtotal": round(subtotal, 6),
    }


def previous_day_bonus(momentum: dict[str, Any] | None) -> float:
    if not momentum:
        return 0.0
    raw = positive_float(momentum.get("previous_day_momentum_score"))
    return min(max(raw, 0.0) / 5.0, 1.0) * PREVIOUS_DAY_BONUS_MAX


def dedupe_same_symbol(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep only the more liquid listing when the same symbol exists on NSE+BSE."""
    chosen: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "")
        current = chosen.get(symbol)
        if current is None:
            chosen[symbol] = candidate
            continue
        duplicates += 1
        candidate_key = (
            positive_float(candidate.get("turnover")),
            -positive_float(candidate.get("spread_pct"), 999.0),
            1 if candidate.get("exchange") == "NSE" else 0,
        )
        current_key = (
            positive_float(current.get("turnover")),
            -positive_float(current.get("spread_pct"), 999.0),
            1 if current.get("exchange") == "NSE" else 0,
        )
        if candidate_key > current_key:
            chosen[symbol] = candidate
    return list(chosen.values()), duplicates


def fetch_previous_day_for_candidates(kite: Any, candidates: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    today = datetime.now(IST).date()
    from_date = today - timedelta(days=HISTORY_LOOKBACK_DAYS)
    to_date = today - timedelta(days=1)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    stats = {"requested": 0, "received": 0, "usable": 0, "failed": 0}
    consecutive_failures = 0

    for index, candidate in enumerate(candidates, 1):
        token = positive_int(candidate.get("instrument_token"))
        if token <= 0:
            stats["failed"] += 1
            continue
        stats["requested"] += 1
        try:
            candles = kite.historical_data(token, from_date, to_date, "day", continuous=False, oi=False)
            stats["received"] += 1
            consecutive_failures = 0
        except Exception:
            stats["failed"] += 1
            consecutive_failures += 1
            if consecutive_failures >= 10:
                raise RuntimeError("Ten consecutive historical-data requests failed")
            time.sleep(HISTORY_DELAY_SECONDS)
            continue

        momentum = calculate_previous_day_momentum(candles)
        if momentum:
            out[(candidate["exchange"], candidate["symbol"])] = momentum
            stats["usable"] += 1
        if index < len(candidates):
            time.sleep(HISTORY_DELAY_SECONDS)
    return out, stats


def main() -> int:
    args = parse_args()
    if args.top <= 0 or args.min_selected <= 0 or args.min_selected > args.top:
        raise SystemExit("ERROR: invalid --top/--min-selected")
    if args.history_candidates < args.top:
        raise SystemExit("ERROR: --history-candidates must be >= --top")

    settings = SelectorSettings(
        top_n=args.top,
        min_selected=args.min_selected,
        min_price=MIN_PRICE,
        max_price=args.max_price,
        min_turnover=args.min_turnover,
        max_spread_pct=args.max_spread_pct,
        min_circuit_distance_pct=args.min_circuit_distance_pct,
        enable_open_equals_low_priority=False,
        min_live_momentum_pct=0.0,
        enable_previous_day_momentum_fallback=False,
    )

    print("Connecting to Kite for READ-ONLY full Zerodha top-60 scan...")
    kite = get_kite_client()
    rows = equity_instruments(kite)
    print("Regular NSE+BSE EQ instruments:", len(rows))

    quote_keys = [f"{row['exchange']}:{row['symbol']}" for row in rows]
    quotes = fetch_full_quotes(kite, quote_keys)

    eligible: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    exchange_eligible: Counter[str] = Counter()

    for row in rows:
        key = f"{row['exchange']}:{row['symbol']}"
        quote = quotes.get(key)
        if not isinstance(quote, dict):
            rejections["missing_quote"] += 1
            continue

        candidate, reason = evaluate_quote(row, quote, settings, allow_missing_depth=args.allow_missing_depth)
        if candidate is None:
            rejections[reason or "unknown"] += 1
            continue

        change_pct = positive_float(candidate.get("change_pct"))
        day_range_pct = positive_float(candidate.get("day_range_pct"))
        if not movement_gate(change_pct, day_range_pct, args.min_abs_change_pct, args.min_day_range_pct):
            rejections["insufficient_live_movement"] += 1
            continue

        candidate["exchange"] = row["exchange"]
        candidate["instrument_token"] = row["instrument_token"]
        candidate.update(live_score_components(candidate, quote))
        eligible.append(candidate)
        exchange_eligible[row["exchange"]] += 1

    deduped, duplicate_listings_removed = dedupe_same_symbol(eligible)
    deduped.sort(key=lambda c: (-positive_float(c.get("live_subtotal")), -positive_float(c.get("turnover")), c["symbol"]))

    history_pool = deduped[: args.history_candidates]
    momentum_map, history_stats = fetch_previous_day_for_candidates(kite, history_pool)

    for candidate in deduped:
        momentum = momentum_map.get((candidate["exchange"], candidate["symbol"]))
        bonus = previous_day_bonus(momentum)
        if momentum:
            candidate.update(momentum)
        candidate["previous_day_bonus_points"] = round(bonus, 6)
        candidate["final_score"] = round(positive_float(candidate.get("live_subtotal")) + bonus, 6)

    selected = sorted(
        deduped,
        key=lambda c: (-positive_float(c.get("final_score")), -positive_float(c.get("turnover")), c["symbol"]),
    )[: args.top]

    status = "success" if len(selected) >= args.min_selected else "failed"
    generated_at = datetime.now(IST).isoformat(timespec="seconds")
    selected_exchange_counts = Counter(c["exchange"] for c in selected)
    open_extreme_counts = Counter(c.get("open_extreme_reason", "NONE") for c in selected)

    report = {
        "status": status,
        "mode": "READ_ONLY",
        "generated_at": generated_at,
        "universe_source": "KITE_NSE_EQ_AND_BSE_EQ",
        "universe_total": len(rows),
        "quotes_received": len(quotes),
        "hard_filter": {
            "min_price": MIN_PRICE,
            "max_price": args.max_price,
            "min_turnover": args.min_turnover,
            "max_spread_pct": args.max_spread_pct,
            "min_circuit_distance_pct": args.min_circuit_distance_pct,
            "min_abs_change_pct": args.min_abs_change_pct,
            "min_day_range_pct": args.min_day_range_pct,
            "movement_rule": "abs(change)>=min_abs_change OR day_range>=min_day_range",
        },
        "score_weights": {
            "turnover": 30,
            "movement": 25,
            "volume": 20,
            "spread": 10,
            "depth": 5,
            "directional_open_extreme": 5,
            "previous_day_momentum": 5,
        },
        "strict_rejections": dict(rejections),
        "eligible_before_symbol_dedupe": len(eligible),
        "eligible_by_exchange": dict(exchange_eligible),
        "duplicate_same_symbol_listings_removed": duplicate_listings_removed,
        "eligible_after_symbol_dedupe": len(deduped),
        "history_pool_size": len(history_pool),
        "history_statistics": history_stats,
        "selected_count": len(selected),
        "selected_exchange_counts": dict(selected_exchange_counts),
        "selected_open_extreme_counts": dict(open_extreme_counts),
        "selected": selected,
    }
    atomic_write_json(args.report, report)
    atomic_write_json(
        args.output,
        {
            "status": status,
            "generated_at": generated_at,
            "watchlist": [{"symbol": c["symbol"], "exchange": c["exchange"]} for c in selected],
            "selected_details": selected,
        },
    )

    print("\n===== FULL ZERODHA TOP-60 MOMENTUM RESULT =====")
    print("Status:", status)
    print("Quotes received:", len(quotes))
    print("Eligible before symbol dedupe:", len(eligible))
    print("Duplicate same-symbol listings removed:", duplicate_listings_removed)
    print("Eligible after symbol dedupe:", len(deduped))
    print("Selected:", len(selected))
    print("Selected by exchange:", dict(selected_exchange_counts))
    print("Open-extreme bonus counts:", dict(open_extreme_counts))
    print("History stats:", history_stats)
    print("\nTOP SELECTION")
    for i, c in enumerate(selected, 1):
        print(
            f"{i:2d}. {c['exchange']}:{c['symbol']:20s} "
            f"score={positive_float(c.get('final_score')):6.2f} "
            f"chg={positive_float(c.get('change_pct')):+6.2f}% "
            f"range={positive_float(c.get('day_range_pct')):5.2f}% "
            f"turnover={positive_float(c.get('turnover')):,.0f} "
            f"bonus={c.get('open_extreme_reason','NONE')}"
        )

    print("\nREAD ONLY: user_config.json and trading services were not changed.")
    print("Output:", args.output)
    print("Report:", args.report)
    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
