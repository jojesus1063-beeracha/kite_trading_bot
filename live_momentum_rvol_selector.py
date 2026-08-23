#!/usr/bin/env python3
"""Build the live NSE Top-120 from the frozen Momentum/RVOL score table."""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PROJECT_DIR = Path(__file__).resolve().parent
STRATEGY_NAME = "NSE_MOMENTUM_RVOL_TOP120"
BASELINE_DAYS = 20
HISTORY_LOOKBACK_DAYS = 45
HISTORY_DELAY_SECONDS = 0.36


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def session_fraction(now: datetime) -> float:
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return min(max((now - start).total_seconds() / (end - start).total_seconds(), 0.0), 1.0)


def momentum_rvol_score(momentum_pct: float, rvol: float) -> int:
    """Apply the frozen score table in declared priority order."""
    momentum = abs(_number(momentum_pct))
    volume = _number(rvol)
    if momentum >= 2.0 or volume >= 3.0:
        return 20
    if 1.0 <= momentum <= 1.5 and 1.5 <= volume <= 2.0:
        return 100
    if 1.0 <= momentum <= 1.5 and 2.0 < volume < 3.0:
        return 90
    if 1.0 <= momentum <= 1.5 and 1.0 <= volume < 1.5:
        return 80
    if 0.75 <= momentum < 1.0 and 1.5 <= volume < 3.0:
        return 70
    if 0.75 <= momentum <= 1.5 and 0.7 <= volume < 1.0:
        return 60
    if 0.5 <= momentum < 1.0 and 1.0 <= volume < 1.5:
        return 50
    if 1.5 < momentum < 2.0 and 1.0 <= volume < 3.0:
        return 35
    return 10


def sweet_spot_distance(momentum_pct: float, rvol: float) -> float:
    """Normalized distance from the centre of the 100-point zone."""
    momentum_distance = abs(abs(_number(momentum_pct)) - 1.25) / 0.25
    rvol_distance = abs(_number(rvol) - 1.75) / 0.25
    return round(math.hypot(momentum_distance, rvol_distance), 8)


def rank_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda row: (
            -int(row["score"]),
            _number(row["sweet_spot_distance"], math.inf),
            -_number(row.get("turnover")),
            str(row["symbol"]),
        ),
    )


def average_daily_volume(kite, token: int, now: datetime) -> float:
    candles = kite.historical_data(
        token,
        now.date() - timedelta(days=HISTORY_LOOKBACK_DAYS),
        now.date() - timedelta(days=1),
        "day",
        continuous=False,
        oi=False,
    )
    volumes = [
        _number(row.get("volume"))
        for row in (candles or [])
        if isinstance(row, dict) and _number(row.get("volume")) > 0
    ][-BASELINE_DAYS:]
    return sum(volumes) / len(volumes) if volumes else 0.0


def evaluate_candidate(row: dict, quote: dict, average_volume: float, fraction: float) -> dict | None:
    ohlc = quote.get("ohlc") or {}
    last_price = _number(quote.get("last_price"))
    previous_close = _number(ohlc.get("close"))
    current_volume = _number(quote.get("volume"))
    if min(last_price, previous_close, current_volume, average_volume, fraction) <= 0:
        return None
    momentum = abs((last_price - previous_close) / previous_close * 100.0)
    expected_volume = average_volume * fraction
    rvol = current_volume / expected_volume
    score = momentum_rvol_score(momentum, rvol)
    return {
        "symbol": row["symbol"],
        "exchange": "NSE",
        "instrument_token": int(row["instrument_token"]),
        "ordinary_equity_clean": True,
        "last_price": round(last_price, 4),
        "momentum_pct": round(momentum, 6),
        "relative_volume": round(rvol, 6),
        "score": score,
        "sweet_spot_distance": sweet_spot_distance(momentum, rvol),
        "current_volume": int(current_volume),
        "average_daily_volume": round(average_volume, 2),
        "expected_volume_now": round(expected_volume, 2),
        "turnover": round(last_price * current_volume, 2),
    }


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.remove(name)


def build_selection(kite, *, top: int, min_selected: int) -> tuple[dict, dict]:
    from paper_full_universe_top60_selector import (
        cleaned_equity_instruments,
        fetch_selector_quotes,
    )

    now = datetime.now(IST)
    fraction = session_fraction(now)
    if fraction <= 0:
        raise RuntimeError("Momentum/RVOL selection cannot run before market open")
    all_rows, cleaning = cleaned_equity_instruments(kite)
    rows = [row for row in all_rows if row["exchange"] == "NSE"]
    quotes = fetch_selector_quotes(kite, [f"NSE:{row['symbol']}" for row in rows])
    candidates = []
    failures = {"missing_quote": 0, "history": 0, "invalid_metrics": 0}
    consecutive_history_failures = 0
    for index, row in enumerate(rows):
        quote = quotes.get(f"NSE:{row['symbol']}")
        if not isinstance(quote, dict):
            failures["missing_quote"] += 1
            continue
        try:
            baseline = average_daily_volume(kite, int(row["instrument_token"]), now)
            consecutive_history_failures = 0
        except Exception:
            failures["history"] += 1
            consecutive_history_failures += 1
            if consecutive_history_failures >= 10:
                raise RuntimeError("Ten consecutive historical-volume requests failed")
            baseline = 0.0
        candidate = evaluate_candidate(row, quote, baseline, fraction)
        if candidate is None:
            failures["invalid_metrics"] += 1
        else:
            candidates.append(candidate)
        if index + 1 < len(rows):
            time.sleep(HISTORY_DELAY_SECONDS)

    selected = rank_candidates(candidates)[:top]
    if len(selected) < min_selected:
        raise RuntimeError(f"Only {len(selected)} scored NSE equities; require {min_selected}")
    generated_at = now.isoformat(timespec="seconds")
    report = {
        "status": "success",
        "mode": "READ_ONLY",
        "strategy": STRATEGY_NAME,
        "generated_at": generated_at,
        "universe_source": "KITE_NSE_ORDINARY_EQUITIES",
        "universe_total": len(rows),
        "quotes_received": len(quotes),
        "session_fraction": round(fraction, 8),
        "baseline_days": BASELINE_DAYS,
        "score_policy": "FROZEN_MOMENTUM_RVOL_TABLE",
        "tie_break": "score_desc,sweet_spot_distance_asc,turnover_desc,symbol_asc",
        "failures": failures,
        "cleaning": cleaning,
        "eligible_count": len(candidates),
        "selected_count": len(selected),
        "selected": selected,
    }
    payload = {
        "status": "success",
        "strategy": STRATEGY_NAME,
        "generated_at": generated_at,
        "watchlist": [{"symbol": row["symbol"], "exchange": "NSE"} for row in selected],
        "selected_details": selected,
    }
    return report, payload


def main() -> int:
    from auth import get_kite_client

    parser = argparse.ArgumentParser()
    runtime = PROJECT_DIR / "runtime" / "live_watchlist"
    parser.add_argument("--top", type=int, default=120)
    parser.add_argument("--min-selected", type=int, default=120)
    parser.add_argument("--output", type=Path, default=runtime / "latest_watchlist.json")
    parser.add_argument("--report", type=Path, default=runtime / "latest_report.json")
    args = parser.parse_args()
    report, payload = build_selection(
        get_kite_client(), top=args.top, min_selected=args.min_selected
    )
    atomic_write(args.report, report)
    atomic_write(args.output, payload)
    print(f"LIVE Momentum/RVOL Top-{args.top}: {report['selected_count']} selected")
    for rank, row in enumerate(report["selected"], 1):
        print(
            f"{rank:3d}. NSE:{row['symbol']:<20} score={row['score']:3d} "
            f"momentum={row['momentum_pct']:6.3f}% rvol={row['relative_volume']:6.3f} "
            f"distance={row['sweet_spot_distance']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
