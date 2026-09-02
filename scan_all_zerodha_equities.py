#!/usr/bin/env python3
"""Read-only scanner for the complete Zerodha NSE + BSE equity universe.

This scanner deliberately does not use the NIFTY 500 universe. It obtains the
instrument master directly from Kite for both NSE and BSE, keeps only regular
equity instruments, then reuses the existing auto-watchlist quote evaluation
and ranking logic. It is read-only unless explicitly extended later.
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
)

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "runtime" / "zerodha_universe" / "top100_all_equities.json"
DEFAULT_REPORT = PROJECT_DIR / "runtime" / "zerodha_universe" / "latest_all_equities_report.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scan all Zerodha NSE/BSE equity instruments.")
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--min-selected", type=int, default=100)
    p.add_argument("--min-price", type=float, default=20.0)
    p.add_argument("--max-price", type=float, default=5000.0)
    p.add_argument("--min-turnover", type=float, default=500_000.0)
    p.add_argument("--max-spread-pct", type=float, default=0.40)
    p.add_argument("--min-circuit-distance-pct", type=float, default=0.75)
    p.add_argument("--open-low-tolerance-ticks", type=int, default=0)
    p.add_argument("--min-live-momentum-pct", type=float, default=0.20)
    p.add_argument("--disable-open-equals-low-priority", action="store_true")
    p.add_argument("--disable-previous-day-fallback", action="store_true")
    p.add_argument("--historical-lookback-days", type=int, default=45)
    p.add_argument("--historical-delay-seconds", type=float, default=0.36)
    p.add_argument("--allow-missing-depth", action="store_true")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return p.parse_args()


def settings_from_args(args: argparse.Namespace) -> SelectorSettings:
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


def equity_instruments(kite: Any) -> list[dict[str, Any]]:
    """Return regular equity instruments from both NSE and BSE.

    We intentionally filter from Kite's instrument master instead of relying
    on a third-party index list. Only exchange cash-market EQ instruments are
    admitted. This excludes futures, options, currencies, commodities and
    other instrument types from the strategy universe.
    """
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for exchange in ("NSE", "BSE"):
        rows = kite.instruments(exchange)
        if not isinstance(rows, list):
            raise AutoWatchlistError(f"Kite {exchange} instrument response was not a list")

        for instrument in rows:
            ex = str(instrument.get("exchange") or "").upper()
            segment = str(instrument.get("segment") or "").upper()
            instrument_type = str(instrument.get("instrument_type") or "").upper()
            symbol = str(instrument.get("tradingsymbol") or "").strip().upper()

            if not symbol or ex != exchange or instrument_type != "EQ":
                continue
            if segment != exchange:
                continue

            key = (exchange, symbol)
            if key in seen:
                continue
            seen.add(key)

            result.append({
                "symbol": symbol,
                "exchange": exchange,
                "company_name": instrument.get("name", ""),
                "industry": "",
                "series": "EQ",
                "isin": instrument.get("isin", ""),
                "instrument_token": positive_int(instrument.get("instrument_token")),
                "tick_size": positive_float(instrument.get("tick_size"), 0.05),
            })

    return result


def main() -> int:
    args = parse_args()
    settings = settings_from_args(args)
    kite = get_kite_client()

    rows = equity_instruments(kite)
    if not rows:
        raise AutoWatchlistError("Kite returned zero NSE/BSE EQ instruments")

    quote_keys = [f"{r['exchange']}:{r['symbol']}" for r in rows]
    quotes = fetch_full_quotes(kite, quote_keys)

    eligible: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    by_exchange = Counter()
    rejected_by_exchange = Counter()

    for row in rows:
        key = f"{row['exchange']}:{row['symbol']}"
        quote = quotes.get(key)
        if not isinstance(quote, dict):
            rejections["missing_quote"] += 1
            rejected_by_exchange[row["exchange"]] += 1
            continue

        candidate, reason = evaluate_quote(
            row, quote, settings, allow_missing_depth=args.allow_missing_depth
        )
        if candidate is None:
            rejections[reason or "unknown_rejection"] += 1
            rejected_by_exchange[row["exchange"]] += 1
            continue

        candidate["exchange"] = row["exchange"]
        eligible.append(candidate)
        by_exchange[row["exchange"]] += 1

    def score_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -positive_float(item.get("score")),
            -positive_float(item.get("turnover")),
            item.get("exchange", ""),
            item.get("symbol", ""),
        )

    priority_1 = [c for c in eligible if c.get("open_equals_low") is True]
    priority_1.sort(key=score_key)
    for c in priority_1:
        c["selection_priority"] = "PRIORITY_1_OPEN_EQUALS_LOW"

    priority_2 = [
        c for c in eligible
        if c.get("open_equals_low") is not True
        and max(abs(positive_float(c.get("change_pct"))), positive_float(c.get("day_range_pct")))
        >= settings.min_live_momentum_pct
    ]
    priority_2.sort(key=score_key)
    for c in priority_2:
        c["selection_priority"] = "PRIORITY_2_LIVE_MOMENTUM"

    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    for candidate in priority_1 + priority_2:
        key = (candidate["exchange"], candidate["symbol"])
        if key in selected_keys:
            continue
        selected.append(candidate)
        selected_keys.add(key)
        if len(selected) >= settings.top_n:
            break

    historical_statistics = {
        "historical_requested": 0,
        "historical_received": 0,
        "historical_usable": 0,
        "historical_failed": 0,
    }
    priority_3: list[dict[str, Any]] = []
    fallback_rejections: Counter[str] = Counter()

    if len(selected) < settings.top_n and settings.enable_previous_day_momentum_fallback:
        momentum_by_symbol, historical_statistics = fetch_previous_day_momentum(
            kite, rows, settings
        )
        for row in rows:
            key = (row["exchange"], row["symbol"])
            if key in selected_keys:
                continue
            # The existing fallback returns momentum keyed by symbol. Avoid
            # ambiguous cross-exchange collisions by only accepting a symbol
            # whose eligible row is unique across NSE/BSE.
            matching = [r for r in rows if r["symbol"] == row["symbol"]]
            if len(matching) != 1:
                fallback_rejections["ambiguous_cross_exchange_symbol"] += 1
                continue
            momentum = momentum_by_symbol.get(row["symbol"])
            if momentum is None:
                fallback_rejections["missing_previous_day_momentum"] += 1
                continue
            quote = quotes.get(f"{row['exchange']}:{row['symbol']}")
            if not isinstance(quote, dict):
                fallback_rejections["missing_current_quote"] += 1
                continue
            candidate, reason = evaluate_quote(
                row, quote, settings, allow_missing_depth=args.allow_missing_depth
            )
            if candidate is None:
                fallback_rejections[reason or "unknown_fallback_rejection"] += 1
                continue
            candidate.update(momentum)
            candidate["exchange"] = row["exchange"]
            candidate["selection_priority"] = "PRIORITY_3_PREVIOUS_DAY_MOMENTUM"
            candidate["live_score"] = candidate["score"]
            candidate["score"] = momentum["previous_day_momentum_score"]
            priority_3.append(candidate)

        priority_3.sort(
            key=lambda x: (
                -positive_float(x.get("previous_day_momentum_score")),
                -abs(positive_float(x.get("previous_day_return_pct"))),
                -positive_float(x.get("previous_day_volume_ratio")),
                x.get("exchange", ""),
                x.get("symbol", ""),
            )
        )
        for candidate in priority_3:
            key = (candidate["exchange"], candidate["symbol"])
            if key in selected_keys:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            if len(selected) >= settings.top_n:
                break

    selected = selected[: settings.top_n]
    priority_counts = Counter(c.get("selection_priority", "UNKNOWN") for c in selected)
    selected_exchange_counts = Counter(c.get("exchange", "UNKNOWN") for c in selected)
    status = "success" if len(selected) >= settings.min_selected else "failed"

    report = {
        "status": status,
        "mode": "READ_ONLY",
        "universe_source": "KITE_NSE_EQ_AND_BSE_EQ",
        "universe_total": len(rows),
        "universe_by_exchange": Counter(r["exchange"] for r in rows),
        "quotes_received": len(quotes),
        "eligible_by_exchange": dict(by_exchange),
        "rejected_by_exchange": dict(rejected_by_exchange),
        "strict_eligible": len(eligible),
        "strict_rejections": dict(rejections),
        "priority_1_count": len(priority_1),
        "priority_2_count": len(priority_2),
        "priority_3_count": len(priority_3),
        "historical_statistics": historical_statistics,
        "fallback_rejections": dict(fallback_rejections),
        "selected_count": len(selected),
        "selected_exchange_counts": dict(selected_exchange_counts),
        "selected_priority_counts": dict(priority_counts),
        "selected": selected,
    }
    report["generated_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
    atomic_write_json(args.report, report)

    payload = {
        "status": status,
        "generated_at": report["generated_at"],
        "watchlist": [
            {"symbol": c["symbol"], "exchange": c["exchange"]}
            for c in selected
        ],
        "selected_details": selected,
    }
    atomic_write_json(args.output, payload)

    print("===== COMPLETE ZERODHA EQUITY UNIVERSE =====")
    print("Mode: READ ONLY")
    print("NSE EQ + BSE EQ instruments:", len(rows))
    print("Universe by exchange:", dict(Counter(r["exchange"] for r in rows)))
    print("Quotes received:", len(quotes))
    print("Strict eligible:", len(eligible))
    print("Eligible by exchange:", dict(by_exchange))
    print("Priority 1:", len(priority_1))
    print("Priority 2:", len(priority_2))
    print("Priority 3:", len(priority_3))
    print("Selected:", len(selected))
    print("Selected by exchange:", dict(selected_exchange_counts))
    print("Status:", status)
    print("\nTOP SELECTION")
    for i, c in enumerate(selected, 1):
        print(
            f"{i:3d}. {c['exchange']:3s}:{c['symbol']:20s} "
            f"priority={c.get('selection_priority','')} "
            f"score={positive_float(c.get('score')):.3f} "
            f"turnover={positive_float(c.get('turnover')):,.0f} "
            f"change={positive_float(c.get('change_pct')):+.2f}%"
        )

    print("\nREAD ONLY: user_config.json was not modified.")
    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
