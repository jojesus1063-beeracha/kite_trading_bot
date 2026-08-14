#!/usr/bin/env python3
"""Build a PAPER watchlist from the top 10 NSE gainers and losers."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auth import get_kite_client
from auto_watchlist import SelectorSettings, atomic_write_json, evaluate_quote
from paper_full_universe_top60_selector import (
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    fetch_selector_quotes,
    ordinary_equity_rejection_reason,
    write_paper_watchlist,
)


IST = ZoneInfo("Asia/Kolkata")
STRATEGY_NAME = "NSE_TOP10_GAINERS_TOP10_LOSERS"
DEFAULT_WINNERS = 10
DEFAULT_LOSERS = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an NSE top-gainers/top-losers PAPER watchlist."
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--winners", type=int, default=DEFAULT_WINNERS)
    parser.add_argument("--losers", type=int, default=DEFAULT_LOSERS)
    parser.add_argument("--min-price", type=float, default=20.0)
    parser.add_argument("--max-price", type=float, default=2200.0)
    parser.add_argument("--min-turnover", type=float, default=1_000_000.0)
    parser.add_argument("--max-spread-pct", type=float, default=0.25)
    parser.add_argument("--min-circuit-distance-pct", type=float, default=1.0)
    parser.add_argument("--allow-missing-depth", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


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
    """Rank positive changes descending and negative changes ascending."""
    gainers = sorted(
        (item for item in candidates if float(item.get("change_pct") or 0.0) > 0),
        key=lambda item: (
            -float(item.get("change_pct") or 0.0),
            -float(item.get("turnover") or 0.0),
            str(item.get("symbol") or ""),
        ),
    )[:winners]
    decliners = sorted(
        (item for item in candidates if float(item.get("change_pct") or 0.0) < 0),
        key=lambda item: (
            float(item.get("change_pct") or 0.0),
            -float(item.get("turnover") or 0.0),
            str(item.get("symbol") or ""),
        ),
    )[:losers]

    for rank, item in enumerate(gainers, 1):
        item["mover_group"] = "GAINER"
        item["mover_rank"] = rank
    for rank, item in enumerate(decliners, 1):
        item["mover_group"] = "LOSER"
        item["mover_rank"] = rank
    return gainers + decliners, gainers, decliners


def main() -> int:
    args = parse_args()
    if args.winners <= 0 or args.losers <= 0:
        raise SystemExit("ERROR: --winners and --losers must be positive")

    settings = SelectorSettings(
        top_n=args.winners + args.losers,
        min_selected=args.winners + args.losers,
        min_price=args.min_price,
        max_price=args.max_price,
        min_turnover=args.min_turnover,
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
        })
        eligible.append(candidate)

    selected, gainers, losers = select_top_movers(
        eligible, args.winners, args.losers
    )
    if len(gainers) != args.winners or len(losers) != args.losers:
        raise SystemExit(
            f"FAIL: require {args.winners} gainers and {args.losers} losers; "
            f"received {len(gainers)} and {len(losers)}"
        )

    generated_at = datetime.now(IST).isoformat(timespec="seconds")
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
        "safety_filters": {
            "min_price": args.min_price,
            "max_price": args.max_price,
            "min_turnover": args.min_turnover,
            "max_spread_pct": args.max_spread_pct,
            "min_circuit_distance_pct": args.min_circuit_distance_pct,
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

    print("\n===== NSE TOP 10 GAINERS + TOP 10 LOSERS =====")
    for item in selected:
        print(
            f"{item['mover_group']:<6} #{item['mover_rank']:02d} "
            f"NSE:{item['symbol']:<20} change={item['change_pct']:+.2f}% "
            f"turnover={item['turnover']:,.0f}"
        )
    print("Watchlist count:", len(selected))
    print("Config backup:", config_backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
