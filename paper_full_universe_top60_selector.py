#!/usr/bin/env python3
"""PAPER-only cleaned full-Zerodha top-60 morning selector.

Universe:
- Kite NSE + BSE cash-market instruments.
- Only ordinary company-equity style rows are retained before quote fetching.
- ETF/fund/debt-like ISINs, special-series suffixes, non-unit lot sizes and
  non-EQ/non-cash rows are rejected fail-closed.

Selection policy (frozen from the verified read-only trial):
- price Rs20-Rs2200
- turnover >= Rs10 lakh
- spread <= 0.25%
- circuit distance >= 1.0%
- abs(change) >= 0.30% OR day range >= 0.40%
- score: turnover 30, movement 25, volume 20, spread 10, depth 5,
  directional Open=Low/Open=High 5, previous-day momentum 5
- deduplicate the same trading symbol across NSE/BSE before final ranking
- final target: 60 unique symbols

Safe by default. user_config.json is changed only with --write, and --write
requires paper_trading=true. This module never starts/stops any service.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auth import get_kite_client
from auto_watchlist import (
    SelectorSettings,
    atomic_write_json,
    evaluate_quote,
    fetch_full_quotes,
    positive_float,
    positive_int,
)
from scan_zerodha_top60_momentum import (
    HISTORY_RECHECK_MULTIPLIER,
    MAX_PRICE,
    MAX_SPREAD_PCT,
    MIN_ABS_CHANGE_PCT,
    MIN_CIRCUIT_DISTANCE_PCT,
    MIN_DAY_RANGE_PCT,
    MIN_PRICE,
    MIN_TURNOVER,
    TOP_N,
    dedupe_same_symbol,
    fetch_previous_day_for_candidates,
    live_score_components,
    movement_gate,
    previous_day_bonus,
)

IST = ZoneInfo("Asia/Kolkata")
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "user_config.json"
DEFAULT_RUNTIME = PROJECT_DIR / "runtime" / "auto_watchlist"
DEFAULT_OUTPUT = DEFAULT_RUNTIME / "latest_watchlist.json"
DEFAULT_REPORT = DEFAULT_RUNTIME / "latest_report.json"

ALLOWED_EXCHANGES = {"NSE", "BSE"}
DISALLOWED_SUFFIXES = (
    "-BE", "-BZ", "-ST", "-SM", "-IL", "-BT", "-GC", "-W1", "-W2"
)
# Secondary defence. The ISIN rule is the primary ETF/fund discriminator:
# ordinary company securities normally arrive with INE..., while ETF/fund
# units commonly arrive with INF... . Avoid broad GOLD/SILVER substrings so
# legitimate names such as GOLDIAM are not rejected.
NON_ORDINARY_SYMBOL_RE = re.compile(r"(?:ETF|IETF|BEES|NETF)$", re.IGNORECASE)
NON_ORDINARY_NAME_RE = re.compile(
    r"\b(?:ETF|EXCHANGE\s+TRADED\s+FUND|MUTUAL\s+FUND|INDEX\s+FUND|"
    r"SOVEREIGN\s+GOLD\s+BOND|TREASURY\s+BILL|GOVERNMENT\s+SECURIT(?:Y|IES))\b",
    re.IGNORECASE,
)
STRATEGY_NAME = "FULL_ZERODHA_CLEAN_TOP60_MOMENTUM"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the cleaned full-Zerodha PAPER top-60 morning watchlist."
    )
    p.add_argument("--write", action="store_true")
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
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return p.parse_args()


def ordinary_equity_rejection_reason(instrument: dict[str, Any], exchange: str) -> str | None:
    ex = str(instrument.get("exchange") or "").upper()
    segment = str(instrument.get("segment") or "").upper()
    instrument_type = str(instrument.get("instrument_type") or "").upper()
    symbol = str(instrument.get("tradingsymbol") or "").strip().upper()
    name = str(instrument.get("name") or "").strip().upper()
    isin = str(instrument.get("isin") or "").strip().upper()
    lot_size = positive_int(instrument.get("lot_size"), 0)

    if exchange not in ALLOWED_EXCHANGES or ex != exchange:
        return "wrong_exchange"
    if segment != exchange:
        return "non_cash_segment"
    if instrument_type != "EQ":
        return "non_eq_instrument_type"
    if not symbol:
        return "blank_symbol"
    if lot_size != 1:
        return "lot_size_not_one"
    if symbol.endswith(DISALLOWED_SUFFIXES):
        return "special_series_suffix"
    if not isin.startswith("INE"):
        return "non_ordinary_isin"
    if NON_ORDINARY_SYMBOL_RE.search(symbol):
        return "fund_like_symbol"
    if NON_ORDINARY_NAME_RE.search(name):
        return "fund_or_debt_like_name"
    return None


def cleaned_equity_instruments(kite: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    raw_by_exchange: Counter[str] = Counter()
    clean_by_exchange: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()

    for exchange in ("NSE", "BSE"):
        instruments = kite.instruments(exchange)
        if not isinstance(instruments, list):
            raise RuntimeError(f"Kite {exchange} instrument response was not a list")
        raw_by_exchange[exchange] += len(instruments)

        for instrument in instruments:
            reason = ordinary_equity_rejection_reason(instrument, exchange)
            if reason is not None:
                rejected[reason] += 1
                continue

            symbol = str(instrument.get("tradingsymbol") or "").strip().upper()
            key = (exchange, symbol)
            if key in seen:
                rejected["duplicate_exchange_symbol_in_master"] += 1
                continue
            seen.add(key)

            cleaned.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "company_name": str(instrument.get("name") or "").strip(),
                    "industry": "",
                    "series": "EQ",
                    "isin": str(instrument.get("isin") or "").strip().upper(),
                    "instrument_token": positive_int(instrument.get("instrument_token")),
                    "tick_size": positive_float(instrument.get("tick_size"), 0.05),
                    "lot_size": positive_int(instrument.get("lot_size"), 1),
                }
            )
            clean_by_exchange[exchange] += 1

    return cleaned, {
        "raw_total": sum(raw_by_exchange.values()),
        "raw_by_exchange": dict(raw_by_exchange),
        "clean_total": len(cleaned),
        "clean_by_exchange": dict(clean_by_exchange),
        "cleaning_rejections": dict(sorted(rejected.items())),
    }


def write_paper_watchlist(
    config_path: Path,
    selected: list[dict[str, Any]],
    *,
    min_selected: int,
    runtime_dir: Path,
) -> Path:
    if len(selected) < min_selected:
        raise RuntimeError(
            f"Refusing to write undersized PAPER watchlist: {len(selected)} < {min_selected}"
        )

    config_path = Path(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("user_config.json is not a JSON object")
    if data.get("paper_trading") is not True:
        raise RuntimeError("SAFETY BLOCK: full-universe selector may write only in PAPER mode")

    watchlist = [
        {"symbol": str(c["symbol"]), "exchange": str(c["exchange"])}
        for c in selected
    ]
    symbols = [item["symbol"] for item in watchlist]
    if len(symbols) != len(set(symbols)):
        raise RuntimeError("Refusing to write duplicate symbols across NSE/BSE")
    if any(item["exchange"] not in ALLOWED_EXCHANGES for item in watchlist):
        raise RuntimeError("Refusing to write a non-NSE/BSE watchlist row")

    runtime_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = runtime_dir / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"user_config-before-full-universe-top60-{stamp}.json"
    shutil.copy2(config_path, backup)

    updated = dict(data)
    updated["watchlist"] = watchlist
    atomic_write_json(config_path, updated)
    return backup


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

    print("Connecting to Kite for cleaned full-universe PAPER selector...")
    kite = get_kite_client()
    rows, cleaning = cleaned_equity_instruments(kite)
    print("Raw Kite instruments:", cleaning["raw_total"])
    print("Clean ordinary equities:", cleaning["clean_total"])
    print("Cleaning rejections:", cleaning["cleaning_rejections"])

    if len(rows) < args.min_selected:
        raise SystemExit("FAIL: cleaned ordinary-equity universe is too small")

    quote_keys = [f"{row['exchange']}:{row['symbol']}" for row in rows]
    quotes = fetch_full_quotes(kite, quote_keys)
    minimum_quotes = math.ceil(len(rows) * 0.90)
    if len(quotes) < minimum_quotes:
        raise SystemExit(
            f"FAIL: incomplete quote coverage {len(quotes)}/{len(rows)}; minimum {minimum_quotes}"
        )

    eligible: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    exchange_eligible: Counter[str] = Counter()

    for row in rows:
        key = f"{row['exchange']}:{row['symbol']}"
        quote = quotes.get(key)
        if not isinstance(quote, dict):
            rejections["missing_quote"] += 1
            continue

        candidate, reason = evaluate_quote(
            row, quote, settings, allow_missing_depth=args.allow_missing_depth
        )
        if candidate is None:
            rejections[reason or "unknown"] += 1
            continue

        change_pct = positive_float(candidate.get("change_pct"))
        day_range_pct = positive_float(candidate.get("day_range_pct"))
        if not movement_gate(
            change_pct,
            day_range_pct,
            args.min_abs_change_pct,
            args.min_day_range_pct,
        ):
            rejections["insufficient_live_movement"] += 1
            continue

        candidate["exchange"] = row["exchange"]
        candidate["instrument_token"] = row["instrument_token"]
        candidate["ordinary_equity_clean"] = True
        candidate.update(live_score_components(candidate, quote))
        eligible.append(candidate)
        exchange_eligible[row["exchange"]] += 1

    deduped, duplicate_listings_removed = dedupe_same_symbol(eligible)
    deduped.sort(
        key=lambda c: (
            -positive_float(c.get("live_subtotal")),
            -positive_float(c.get("turnover")),
            c["symbol"],
        )
    )

    history_pool = deduped[: args.history_candidates]
    momentum_map, history_stats = fetch_previous_day_for_candidates(kite, history_pool)
    for candidate in deduped:
        momentum = momentum_map.get((candidate["exchange"], candidate["symbol"]))
        bonus = previous_day_bonus(momentum)
        if momentum:
            candidate.update(momentum)
        candidate["previous_day_bonus_points"] = round(bonus, 6)
        candidate["final_score"] = round(
            positive_float(candidate.get("live_subtotal")) + bonus, 6
        )

    selected = sorted(
        deduped,
        key=lambda c: (
            -positive_float(c.get("final_score")),
            -positive_float(c.get("turnover")),
            c["symbol"],
        ),
    )[: args.top]

    if len(selected) < args.min_selected:
        raise SystemExit(
            f"FAIL: only {len(selected)} qualified clean symbols; require {args.min_selected}"
        )

    generated_at = datetime.now(IST).isoformat(timespec="seconds")
    selected_exchange_counts = Counter(c["exchange"] for c in selected)
    open_extreme_counts = Counter(c.get("open_extreme_reason", "NONE") for c in selected)

    config_backup = None
    if args.write:
        config_backup = write_paper_watchlist(
            args.config,
            selected,
            min_selected=args.min_selected,
            runtime_dir=args.report.parent,
        )

    report = {
        "status": "success",
        "paper_only": True,
        "strategy": STRATEGY_NAME,
        "mode": "WRITE_CONFIG" if args.write else "READ_ONLY",
        "generated_at": generated_at,
        "universe_source": "KITE_CLEAN_ORDINARY_NSE_BSE_EQUITIES",
        "cleaning": cleaning,
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
        "strict_rejections": dict(sorted(rejections.items())),
        "eligible_before_symbol_dedupe": len(eligible),
        "eligible_by_exchange": dict(exchange_eligible),
        "duplicate_same_symbol_listings_removed": duplicate_listings_removed,
        "eligible_after_symbol_dedupe": len(deduped),
        "history_pool_size": len(history_pool),
        "history_statistics": history_stats,
        "selected_count": len(selected),
        "selected_exchange_counts": dict(selected_exchange_counts),
        "selected_open_extreme_counts": dict(open_extreme_counts),
        "config_backup": None if config_backup is None else str(config_backup),
        "selected": selected,
    }

    payload = {
        "status": "success",
        "paper_only": True,
        "strategy": STRATEGY_NAME,
        "generated_at": generated_at,
        "watchlist": [
            {"symbol": c["symbol"], "exchange": c["exchange"]}
            for c in selected
        ],
        "selected_details": selected,
    }
    atomic_write_json(args.report, report)
    atomic_write_json(args.output, payload)

    print("\n===== CLEAN FULL-ZERODHA PAPER TOP-60 =====")
    print("Status: success")
    print("Mode:", report["mode"])
    print("Quotes received:", len(quotes))
    print("Eligible before dedupe:", len(eligible))
    print("Duplicate same-symbol listings removed:", duplicate_listings_removed)
    print("Eligible after dedupe:", len(deduped))
    print("Selected:", len(selected))
    print("Selected by exchange:", dict(selected_exchange_counts))
    print("Open-extreme bonus counts:", dict(open_extreme_counts))
    print("History stats:", history_stats)
    print("\nTOP 60")
    for i, c in enumerate(selected, 1):
        print(
            f"{i:2d}. {c['exchange']}:{c['symbol']:20s} "
            f"score={positive_float(c.get('final_score')):6.2f} "
            f"chg={positive_float(c.get('change_pct')):+6.2f}% "
            f"range={positive_float(c.get('day_range_pct')):5.2f}% "
            f"turnover={positive_float(c.get('turnover')):,.0f} "
            f"bonus={c.get('open_extreme_reason','NONE')}"
        )

    if args.write:
        print("\nPAPER CONFIG UPDATED atomically; backup:", config_backup)
    else:
        print("\nREAD ONLY: user_config.json was not modified.")
    print("Output:", args.output)
    print("Report:", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
