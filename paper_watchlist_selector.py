#!/usr/bin/env python3
"""Paper-only momentum + volume-famine watchlist selector.

This selector deliberately has only two strategy gates:
1) high intraday momentum
2) temporary time-adjusted volume famine

It does not use Open=Low, EMA, ADX, VWAP, previous-day momentum,
price-action, sector, market-alignment, or fallback filling.

Basic data-validity checks remain so malformed quotes are not selected.
The script refuses to write a watchlist unless config.PAPER_TRADING is True.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import config as cfg
from auth import get_kite_client
from auto_watchlist import download_nifty500, usable_nse_equity_instruments


IST = ZoneInfo("Asia/Kolkata")
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "user_config.json"
DEFAULT_RUNTIME_DIR = PROJECT_DIR / "runtime" / "paper_watchlist"
DEFAULT_REPORT_PATH = DEFAULT_RUNTIME_DIR / "latest_report.json"
DEFAULT_OUTPUT_PATH = DEFAULT_RUNTIME_DIR / "latest_watchlist.json"

QUOTE_BATCH_SIZE = 500
QUOTE_BATCH_DELAY_SECONDS = 1.10


class PaperWatchlistError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperSelectorSettings:
    momentum_min_pct: float = 0.75
    famine_rvol_min: float = 0.40
    famine_rvol_max: float = 0.70
    baseline_days: int = 20
    historical_lookback_days: int = 45
    historical_delay_seconds: float = 0.36
    earliest_famine_time: str = "09:45"
    top_n: int = 60


def positive_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def positive_int(value: Any, default: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def now_ist() -> datetime:
    return datetime.now(IST)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def elapsed_session_fraction(now: datetime) -> float:
    """Fraction of regular NSE cash session elapsed, clamped to [0, 1]."""
    open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
    total = (close_dt - open_dt).total_seconds()
    elapsed = (now - open_dt).total_seconds()
    if total <= 0:
        return 0.0
    return min(max(elapsed / total, 0.0), 1.0)


def after_earliest_famine_time(now: datetime, hhmm: str) -> bool:
    hour, minute = [int(part) for part in hhmm.split(":", 1)]
    gate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= gate


def fetch_quotes(kite: Any, quote_keys: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    batches = list(chunks(quote_keys, QUOTE_BATCH_SIZE))
    for index, batch in enumerate(batches):
        if index:
            time.sleep(QUOTE_BATCH_DELAY_SECONDS)
        response = kite.quote(batch)
        if not isinstance(response, dict):
            raise PaperWatchlistError("Kite quote response was not a dictionary")
        result.update(response)
    return result


def average_daily_volume(kite: Any, token: int, settings: PaperSelectorSettings) -> float:
    today = now_ist().date()
    candles = kite.historical_data(
        token,
        today - timedelta(days=settings.historical_lookback_days),
        today - timedelta(days=1),
        "day",
        continuous=False,
        oi=False,
    )
    volumes = [
        positive_int(candle.get("volume"))
        for candle in candles
        if isinstance(candle, dict) and positive_int(candle.get("volume")) > 0
    ]
    volumes = volumes[-settings.baseline_days :]
    if not volumes:
        return 0.0
    return sum(volumes) / len(volumes)


def evaluate_candidate(
    row: dict[str, Any],
    quote: dict[str, Any],
    average_volume: float,
    session_fraction: float,
    settings: PaperSelectorSettings,
) -> dict[str, Any] | None:
    last_price = positive_float(quote.get("last_price"))
    current_volume = positive_int(quote.get("volume"))
    ohlc = quote.get("ohlc") or {}
    previous_close = positive_float(ohlc.get("close"))
    high = positive_float(ohlc.get("high"))
    low = positive_float(ohlc.get("low"))

    # Data validity only; these are not strategy filters.
    if last_price <= 0 or previous_close <= 0 or current_volume <= 0:
        return None
    if high <= 0 or low <= 0 or average_volume <= 0 or session_fraction <= 0:
        return None

    change_pct = ((last_price - previous_close) / previous_close) * 100.0
    day_range_pct = ((high - low) / previous_close) * 100.0
    momentum_pct = max(abs(change_pct), day_range_pct)

    expected_volume_now = average_volume * session_fraction
    if expected_volume_now <= 0:
        return None

    relative_volume = current_volume / expected_volume_now

    # The only strategy gates.
    if momentum_pct < settings.momentum_min_pct:
        return None
    if not settings.famine_rvol_min <= relative_volume <= settings.famine_rvol_max:
        return None

    # Ranking uses only the two selected concepts: momentum and famine.
    famine_mid = (settings.famine_rvol_min + settings.famine_rvol_max) / 2.0
    famine_width = max((settings.famine_rvol_max - settings.famine_rvol_min) / 2.0, 1e-9)
    famine_quality = max(0.0, 1.0 - abs(relative_volume - famine_mid) / famine_width)
    score = momentum_pct * (1.0 + famine_quality)

    return {
        "symbol": row["symbol"],
        "exchange": "NSE",
        "instrument_token": positive_int(row.get("instrument_token")),
        "last_price": round(last_price, 4),
        "change_pct": round(change_pct, 4),
        "day_range_pct": round(day_range_pct, 4),
        "momentum_pct": round(momentum_pct, 4),
        "current_volume": current_volume,
        "average_daily_volume": round(average_volume, 2),
        "expected_volume_now": round(expected_volume_now, 2),
        "relative_volume": round(relative_volume, 4),
        "famine": True,
        "high_momentum": True,
        "score": round(score, 6),
    }


def generate_selection(kite: Any, settings: PaperSelectorSettings) -> dict[str, Any]:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise PaperWatchlistError(
            "Refusing to run paper selector because PAPER_TRADING is not True"
        )

    now = now_ist()
    if not after_earliest_famine_time(now, settings.earliest_famine_time):
        raise PaperWatchlistError(
            f"Volume-famine selection is disabled before {settings.earliest_famine_time} IST"
        )

    fraction = elapsed_session_fraction(now)
    universe = download_nifty500()
    raw_instruments = kite.instruments("NSE")
    instrument_map = usable_nse_equity_instruments(raw_instruments)

    matched: list[dict[str, Any]] = []
    for row in universe:
        instrument = instrument_map.get(row["symbol"])
        if instrument is None:
            continue
        token = positive_int(instrument.get("instrument_token"))
        if token <= 0:
            continue
        matched.append({**row, "instrument_token": token})

    quote_keys = [f"NSE:{row['symbol']}" for row in matched]
    quotes = fetch_quotes(kite, quote_keys)

    selected: list[dict[str, Any]] = []
    baseline_failures = 0

    for index, row in enumerate(matched):
        quote = quotes.get(f"NSE:{row['symbol']}")
        if not isinstance(quote, dict):
            continue

        try:
            baseline = average_daily_volume(
                kite,
                positive_int(row.get("instrument_token")),
                settings,
            )
        except Exception:
            baseline_failures += 1
            baseline = 0.0

        candidate = evaluate_candidate(row, quote, baseline, fraction, settings)
        if candidate is not None:
            selected.append(candidate)

        if index + 1 < len(matched):
            time.sleep(settings.historical_delay_seconds)

    selected.sort(
        key=lambda item: (
            -positive_float(item.get("score")),
            -positive_float(item.get("momentum_pct")),
            item["symbol"],
        )
    )

    # No fallback filling. top_n is a cap only.
    selected = selected[: settings.top_n]

    return {
        "status": "success",
        "paper_only": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "strategy": "HIGH_MOMENTUM_AND_VOLUME_FAMINE_ONLY",
        "settings": asdict(settings),
        "statistics": {
            "universe_rows": len(universe),
            "matched_symbols": len(matched),
            "quotes_received": len(quotes),
            "baseline_failures": baseline_failures,
            "selected_symbols": len(selected),
            "session_fraction": round(fraction, 6),
        },
        "selected": selected,
    }


def write_watchlist(config_path: Path, selected: list[dict[str, Any]]) -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise PaperWatchlistError("Refusing write because PAPER_TRADING is not True")

    payload = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    payload["watchlist"] = [
        {"symbol": item["symbol"], "exchange": "NSE"}
        for item in selected
    ]
    atomic_write_json(config_path, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-only momentum + famine watchlist selector")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--momentum-min-pct", type=float, default=0.75)
    parser.add_argument("--famine-rvol-min", type=float, default=0.40)
    parser.add_argument("--famine-rvol-max", type=float, default=0.70)
    parser.add_argument("--baseline-days", type=int, default=20)
    parser.add_argument("--historical-lookback-days", type=int, default=45)
    parser.add_argument("--historical-delay-seconds", type=float, default=0.36)
    parser.add_argument("--earliest-famine-time", default="09:45")
    parser.add_argument("--top", type=int, default=60)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.momentum_min_pct < 0:
        raise SystemExit("ERROR: momentum threshold cannot be negative")
    if not 0 < args.famine_rvol_min <= args.famine_rvol_max:
        raise SystemExit("ERROR: invalid famine RVOL range")
    if args.top <= 0:
        raise SystemExit("ERROR: --top must be positive")
    if args.baseline_days <= 0:
        raise SystemExit("ERROR: --baseline-days must be positive")
    if args.historical_delay_seconds < 0.34:
        raise SystemExit("ERROR: historical delay must be at least 0.34 seconds")

    settings = PaperSelectorSettings(
        momentum_min_pct=args.momentum_min_pct,
        famine_rvol_min=args.famine_rvol_min,
        famine_rvol_max=args.famine_rvol_max,
        baseline_days=args.baseline_days,
        historical_lookback_days=args.historical_lookback_days,
        historical_delay_seconds=args.historical_delay_seconds,
        earliest_famine_time=args.earliest_famine_time,
        top_n=args.top,
    )

    try:
        kite = get_kite_client()
        result = generate_selection(kite, settings)
        atomic_write_json(args.report, result)
        atomic_write_json(
            args.output,
            {
                "status": "success",
                "generated_at": result["generated_at"],
                "watchlist": [
                    {"symbol": item["symbol"], "exchange": "NSE"}
                    for item in result["selected"]
                ],
                "selected_details": result["selected"],
            },
        )

        print("PAPER WATCHLIST: momentum + famine only")
        print("Selected:", result["statistics"]["selected_symbols"])
        for item in result["selected"]:
            print(
                f"{item['symbol']:<14} momentum={item['momentum_pct']:>7.3f}% "
                f"rvol={item['relative_volume']:>6.3f} score={item['score']:>8.3f}"
            )

        if args.write:
            write_watchlist(args.config, result["selected"])
            print("Paper watchlist written to:", args.config)
        else:
            print("DRY RUN: configuration not modified")
        return 0

    except Exception as exc:
        failure = {
            "status": "failed",
            "paper_only": True,
            "generated_at": now_ist().isoformat(timespec="seconds"),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "configuration_changed": False,
        }
        try:
            atomic_write_json(args.report, failure)
        except Exception:
            pass
        print("PAPER WATCHLIST FAILED")
        print("Error:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
