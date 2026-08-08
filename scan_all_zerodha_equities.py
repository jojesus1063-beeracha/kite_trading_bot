#!/usr/bin/env python3
"""
Read-only scanner for the full NSE equity universe exposed by Kite.

Purpose:
    Measure how many Zerodha-listed NSE cash-equity symbols pass the existing
    auto-watchlist tradability/liquidity/activity filters and select the top N.

Important:
    - Reuses the existing evaluate_quote(), SelectorSettings and write helper.
    - Does NOT create a second filter implementation.
    - Does NOT modify user_config.json unless --write is explicitly supplied.
    - Does NOT start/restart the trading bot.
    - Default top N is 100.

This is deliberately separate from auto_watchlist.py because that module's
production universe is currently NIFTY 500. This scanner uses Kite's complete
NSE EQ instrument list as the candidate universe.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from auth import get_kite_client
from auto_watchlist import (
    AutoWatchlistError,
    SelectorSettings,
    atomic_write_json,
    evaluate_quote,
    fetch_full_quotes,
    fetch_previous_day_momentum,
    positive_float,
    positive_int,
    usable_nse_equity_instruments,
    write_watchlist_to_config,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "user_config.json"
DEFAULT_OUTPUT = PROJECT_DIR / "runtime" / "zerodha_universe" / "top100.json"
DEFAULT_REPORT = PROJECT_DIR / "runtime" / "zerodha_universe" / "latest_report.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan all Zerodha NSE EQ instruments using existing watchlist filters."
    )
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--min-selected", type=int, default=100)
    parser.add_argument("--min-price", type=float, default=20.0)
    parser.add_argument("--max-price", type=float, default=5000.0)
    parser.add_argument("--min-turnover", type=float, default=500_000.0)
    parser.add_argument("--max-spread-pct", type=float, default=0.40)
    parser.add_argument("--min-circuit-distance-pct", type=float, default=0.75)
    parser.add_argument("--open-low-tolerance-ticks", type=int, default=0)
    parser.add_argument("--min-live-momentum-pct", type=float, default=0.20)
    parser.add_argument("--disable-open-equals-low-priority", action="store_true")
    parser.add_argument("--disable-previous-day-fallback", action="store_true")
    parser.add_argument("--historical-lookback-days", type=int, default=45)
    parser.add_argument("--historical-delay-seconds", type=float, default=0.36)
    parser.add_argument("--allow-missing-depth", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> SelectorSettings:
    if args.top <= 0:
        raise SystemExit("ERROR: --top must be greater than zero")
    if args.min_selected <= 0 or args.min_selected > args.top:
        raise SystemExit("ERROR: --min-selected must be > 0 and <= --top")
    if args.historical_delay_seconds < 0.34:
        raise SystemExit("ERROR: --historical-delay-seconds must be at least 0.34")

    return SelectorSettings(
        top_n=args.top,
        min_selected=args.min_selected,
        min_price=args.min_price,
        max_price=args.max_price,
        min_turnover=args.min_turnover,
        max_spread_pct=args.max_spread_pct,
        min_circuit_distance_pct=args.min_circuit_distance_pct,
        enable_open_equals_low_priority=not args.disable_open_equals_low_priority,
        open_low_tolerance_ticks=args.open_low_tolerance_ticks,
        min_live_momentum_pct=args.min_live_momentum_pct,
        enable_previous_day_momentum_fallback=not args.disable_previous_day_fallback,
        historical_lookback_days=args.historical_lookback_days,
        historical_delay_seconds=args.historical_delay_seconds,
    )


def main() -> int:
    args = parse_args()
    settings = build_settings(args)

    if args.write and args.allow_missing_depth:
        raise SystemExit("ERROR: --allow-missing-depth cannot be combined with --write")

    kite = get_kite_client()

    raw_instruments = kite.instruments("NSE")
    if not isinstance(raw_instruments, list):
        raise AutoWatchlistError("Kite NSE instrument response was not a list")

    instrument_map = usable_nse_equity_instruments(raw_instruments)
    matched_rows: list[dict[str, Any]] = []

    for symbol, instrument in sorted(instrument_map.items()):
        matched_rows.append(
            {
                "symbol": symbol,
                "exchange": "NSE",
                "company_name": instrument.get("name", ""),
                "industry": "",
                "series": "EQ",
                "isin": instrument.get("isin", ""),
                "instrument_token": positive_int(instrument.get("instrument_token")),
                "tick_size": positive_float(instrument.get("tick_size"), 0.05),
            }
        )

    if not matched_rows:
        raise AutoWatchlistError("Kite returned zero usable NSE EQ instruments")

    quote_keys = [f"NSE:{row['symbol']}" for row in matched_rows]
    quotes = fetch_full_quotes(kite, quote_keys)

    strict_eligible: list[dict[str, Any]] = []
    strict_rejections: Counter[str] = Counter()

    for row in matched_rows:
        quote = quotes.get(f"NSE:{row['symbol']}")
        if not isinstance(quote, dict):
            strict_rejections["missing_quote"] += 1
            continue

        candidate, reason = evaluate_quote(
            row,
            quote,
            settings,
            allow_missing_depth=args.allow_missing_depth,
        )
        if candidate is None:
            strict_rejections[reason or "unknown_rejection"] += 1
            continue
        strict_eligible.append(candidate)

    def live_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -positive_float(item.get("score")),
            -positive_float(item.get("turnover")),
            item["symbol"],
        )

    priority_1 = [
        c for c in strict_eligible if c.get("open_equals_low") is True
    ]
    priority_1.sort(key=live_sort_key)
    for c in priority_1:
        c["selection_priority"] = "PRIORITY_1_OPEN_EQUALS_LOW"

    priority_2 = [
        c
        for c in strict_eligible
        if c.get("open_equals_low") is not True
        and max(abs(positive_float(c.get("change_pct"))), positive_float(c.get("day_range_pct")))
        >= settings.min_live_momentum_pct
    ]
    priority_2.sort(key=live_sort_key)
    for c in priority_2:
        c["selection_priority"] = "PRIORITY_2_LIVE_MOMENTUM"

    selected: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    for candidate in priority_1 + priority_2:
        symbol = candidate["symbol"]
        if symbol in selected_symbols:
            continue
        selected.append(candidate)
        selected_symbols.add(symbol)
        if len(selected) >= settings.top_n:
            break

    historical_statistics = {
        "historical_requested": 0,
        "historical_received": 0,
        "historical_usable": 0,
        "historical_failed": 0,
    }
    fallback_rejections: Counter[str] = Counter()
    priority_3: list[dict[str, Any]] = []

    if (
        len(selected) < settings.top_n
        and settings.enable_previous_day_momentum_fallback
    ):
        momentum_by_symbol, historical_statistics = fetch_previous_day_momentum(
            kite,
            matched_rows,
            settings,
        )
        fallback_settings = settings.__class__(
            **{
                **settings.__dict__,
                "min_turnover": 0.0,
            }
        )

        for row in matched_rows:
            symbol = row["symbol"]
            if symbol in selected_symbols:
                continue
            momentum = momentum_by_symbol.get(symbol)
            if momentum is None:
                fallback_rejections["missing_previous_day_momentum"] += 1
                continue
            quote = quotes.get(f"NSE:{symbol}")
            if not isinstance(quote, dict):
                fallback_rejections["missing_current_quote"] += 1
                continue

            candidate, reason = evaluate_quote(
                row,
                quote,
                fallback_settings,
                allow_missing_depth=args.allow_missing_depth,
            )
            if candidate is None:
                fallback_rejections[reason or "unknown_fallback_rejection"] += 1
                continue

            candidate.update(momentum)
            candidate["selection_priority"] = "PRIORITY_3_PREVIOUS_DAY_MOMENTUM"
            candidate["live_score"] = candidate["score"]
            candidate["score"] = momentum["previous_day_momentum_score"]
            priority_3.append(candidate)

        priority_3.sort(
            key=lambda item: (
                -positive_float(item.get("previous_day_momentum_score")),
                -abs(positive_float(item.get("previous_day_return_pct"))),
                -positive_float(item.get("previous_day_volume_ratio")),
                item["symbol"],
            )
        )
        for candidate in priority_3:
            symbol = candidate["symbol"]
            if symbol in selected_symbols:
                continue
            selected.append(candidate)
            selected_symbols.add(symbol)
            if len(selected) >= settings.top_n:
                break

    selected = selected[: settings.top_n]

    selected_priority_counts = Counter(
        item.get("selection_priority", "UNKNOWN") for item in selected
    )

    status = "success" if len(selected) >= settings.min_selected else "failed"
    report = {
        "status": status,
        "mode": "WRITE" if args.write else "READ_ONLY",
        "universe_source": "KITE_NSE_EQ_INSTRUMENTS",
        "universe_total": len(matched_rows),
        "quotes_received": len(quotes),
        "strict_eligible": len(strict_eligible),
        "strict_rejections": dict(strict_rejections),
        "priority_1_count": len(priority_1),
        "priority_2_count": len(priority_2),
        "priority_3_count": len(priority_3),
        "historical_statistics": historical_statistics,
        "fallback_rejections": dict(fallback_rejections),
        "selected_count": len(selected),
        "selected_priority_counts": dict(selected_priority_counts),
        "selected": selected,
    }

    atomic_write_json(args.report, report)

    print("===== ZERODHA NSE EQ UNIVERSE SCAN =====")
    print("Mode:", "WRITE" if args.write else "READ ONLY")
    print("Zerodha NSE EQ instruments:", len(matched_rows))
    print("Quotes received:", len(quotes))
    print("Strict eligible:", len(strict_eligible))
    print("Priority 1:", len(priority_1))
    print("Priority 2:", len(priority_2))
    print("Priority 3:", len(priority_3))
    print("Selected:", len(selected))
    print("Status:", status)
    print("Rejections:", dict(strict_rejections))
    print("\nTOP SELECTION")
    for index, item in enumerate(selected, 1):
        print(
            f"{index:3d}. {item['symbol']:20s} "
            f"priority={item.get('selection_priority','')} "
            f"score={positive_float(item.get('score')):.3f} "
            f"turnover={positive_float(item.get('turnover')):,.0f} "
            f"change={positive_float(item.get('change_pct')):+.2f}%"
        )

    payload = {
        "status": status,
        "generated_at": report.get("generated_at"),
        "watchlist": [
            {"symbol": item["symbol"], "exchange": "NSE"}
            for item in selected
        ],
        "selected_details": selected,
    }
    atomic_write_json(args.output, payload)

    if args.write:
        if status != "success":
            print("Configuration NOT changed: minimum selection was not met.")
            return 2
        backup = write_watchlist_to_config(
            args.config,
            selected,
            min_selected=settings.min_selected,
            runtime_dir=args.report.parent,
        )
        print("Configuration updated:", args.config)
        print("Backup:", backup)
    else:
        print("\nREAD ONLY: user_config.json was not modified.")

    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
