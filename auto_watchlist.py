#!/usr/bin/env python3
"""
Stage 6 automatic morning watchlist selector.

Safe default behaviour:
- Downloads the official NIFTY 500 constituent CSV.
- Matches constituents with current Kite NSE equity instruments.
- Retrieves full Kite quote snapshots.
- Filters and ranks liquid, active stocks.
- Produces a report.
- Does NOT modify user_config.json unless --write is explicitly supplied.
- Does NOT start the trading bot.

Production sequencing is handled by systemd only after successful selection.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from auth import get_kite_client


IST = ZoneInfo("Asia/Kolkata")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "user_config.json"
DEFAULT_RUNTIME_DIR = PROJECT_DIR / "runtime" / "auto_watchlist"
DEFAULT_REPORT_PATH = DEFAULT_RUNTIME_DIR / "latest_report.json"
DEFAULT_OUTPUT_PATH = DEFAULT_RUNTIME_DIR / "latest_watchlist.json"

NIFTY_500_URL = (
    "https://nsearchives.nseindia.com/"
    "content/indices/ind_nifty500list.csv"
)

QUOTE_BATCH_SIZE = 500
QUOTE_BATCH_DELAY_SECONDS = 1.10


class AutoWatchlistError(RuntimeError):
    """Raised when a valid automatic watchlist cannot be generated."""


@dataclass(frozen=True)
class SelectorSettings:
    top_n: int = 80
    min_selected: int = 80
    min_price: float = 20.0
    max_price: float = 5000.0
    min_turnover: float = 500_000.0
    max_spread_pct: float = 0.40
    min_circuit_distance_pct: float = 0.75

    # Priority 1: strict Open = Low using the instrument tick size.
    enable_open_equals_low_priority: bool = True
    open_low_tolerance_ticks: int = 0

    # Priority 2: minimum current movement required for live momentum.
    min_live_momentum_pct: float = 0.20

    # Priority 3: use previous completed trading-day momentum.
    enable_previous_day_momentum_fallback: bool = True
    historical_lookback_days: int = 45
    historical_delay_seconds: float = 0.36


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def positive_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default

    if not math.isfinite(result):
        return default

    return result


def positive_int(value: Any, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default

    return max(result, 0)


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON through a temporary file and atomic rename."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )

    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                ensure_ascii=False,
                sort_keys=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def parse_nifty500_csv(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))

    required_columns = {
        "Company Name",
        "Industry",
        "Symbol",
        "Series",
        "ISIN Code",
    }

    available_columns = set(reader.fieldnames or [])

    missing_columns = required_columns - available_columns
    if missing_columns:
        raise AutoWatchlistError(
            "NIFTY 500 CSV is missing columns: "
            + ", ".join(sorted(missing_columns))
        )

    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw_row in reader:
        symbol = str(raw_row.get("Symbol") or "").strip().upper()
        series = str(raw_row.get("Series") or "").strip().upper()

        if not symbol or series != "EQ" or symbol in seen:
            continue

        seen.add(symbol)

        rows.append(
            {
                "symbol": symbol,
                "company_name": str(
                    raw_row.get("Company Name") or ""
                ).strip(),
                "industry": str(
                    raw_row.get("Industry") or ""
                ).strip(),
                "series": series,
                "isin": str(
                    raw_row.get("ISIN Code") or ""
                ).strip(),
            }
        )

    if len(rows) < 400:
        raise AutoWatchlistError(
            f"Expected a NIFTY 500-sized universe; received {len(rows)} rows"
        )

    return rows


def download_nifty500(
    url: str = NIFTY_500_URL,
    timeout_seconds: int = 45,
) -> list[dict[str, str]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/130 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=timeout_seconds,
    )
    response.raise_for_status()

    text = response.content.decode("utf-8-sig")

    if not text.strip():
        raise AutoWatchlistError("Downloaded NIFTY 500 CSV was empty")

    return parse_nifty500_csv(text)


def usable_nse_equity_instruments(
    instruments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    for instrument in instruments:
        exchange = str(instrument.get("exchange") or "").upper()
        segment = str(instrument.get("segment") or "").upper()
        instrument_type = str(
            instrument.get("instrument_type") or ""
        ).upper()
        symbol = str(
            instrument.get("tradingsymbol") or ""
        ).strip().upper()

        if (
            exchange != "NSE"
            or segment != "NSE"
            or instrument_type != "EQ"
            or not symbol
        ):
            continue

        result[symbol] = instrument

    return result


def fetch_full_quotes(
    kite: Any,
    quote_keys: list[str],
) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}

    batches = list(chunks(quote_keys, QUOTE_BATCH_SIZE))

    for batch_number, batch in enumerate(batches):
        if batch_number:
            time.sleep(QUOTE_BATCH_DELAY_SECONDS)

        response = kite.quote(batch)

        if not isinstance(response, dict):
            raise AutoWatchlistError(
                "Kite quote response was not a dictionary"
            )

        quotes.update(response)

    return quotes


def best_bid_ask(
    quote: dict[str, Any],
) -> tuple[float, float, int, int]:
    depth = quote.get("depth") or {}
    buy_levels = depth.get("buy") or []
    sell_levels = depth.get("sell") or []

    best_bid = 0.0
    best_ask = 0.0
    bid_quantity = 0
    ask_quantity = 0

    for level in buy_levels:
        price = positive_float(level.get("price"))
        if price > 0:
            best_bid = price
            bid_quantity = positive_int(level.get("quantity"))
            break

    for level in sell_levels:
        price = positive_float(level.get("price"))
        if price > 0:
            best_ask = price
            ask_quantity = positive_int(level.get("quantity"))
            break

    return best_bid, best_ask, bid_quantity, ask_quantity


def evaluate_quote(
    universe_row: dict[str, str],
    quote: dict[str, Any],
    settings: SelectorSettings,
    *,
    allow_missing_depth: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    symbol = universe_row["symbol"]

    last_price = positive_float(quote.get("last_price"))
    volume = positive_int(quote.get("volume"))
    average_price = positive_float(quote.get("average_price"))

    ohlc = quote.get("ohlc") or {}
    open_price = positive_float(ohlc.get("open"))
    high_price = positive_float(ohlc.get("high"))
    low_price = positive_float(ohlc.get("low"))
    previous_close = positive_float(ohlc.get("close"))

    if last_price <= 0:
        return None, "invalid_last_price"

    if previous_close <= 0:
        return None, "invalid_previous_close"

    if open_price <= 0 or high_price <= 0 or low_price <= 0:
        return None, "invalid_ohlc"

    if not settings.min_price <= last_price <= settings.max_price:
        return None, "outside_price_range"

    if volume <= 0:
        return None, "zero_volume"

    turnover_price = average_price if average_price > 0 else last_price
    turnover = volume * turnover_price

    if turnover < settings.min_turnover:
        return None, "turnover_too_low"

    (
        best_bid,
        best_ask,
        bid_quantity,
        ask_quantity,
    ) = best_bid_ask(quote)

    spread_pct: float | None

    if best_bid > 0 and best_ask > 0:
        midpoint = (best_bid + best_ask) / 2.0

        if midpoint <= 0 or best_ask < best_bid:
            return None, "invalid_market_depth"

        spread_pct = ((best_ask - best_bid) / midpoint) * 100.0

        if spread_pct > settings.max_spread_pct:
            return None, "spread_too_wide"

    elif allow_missing_depth:
        spread_pct = None

    else:
        return None, "missing_market_depth"

    upper_circuit = positive_float(
        quote.get("upper_circuit_limit")
    )
    lower_circuit = positive_float(
        quote.get("lower_circuit_limit")
    )

    circuit_distances: list[float] = []

    if upper_circuit > 0:
        circuit_distances.append(
            ((upper_circuit - last_price) / last_price) * 100.0
        )

    if lower_circuit > 0:
        circuit_distances.append(
            ((last_price - lower_circuit) / last_price) * 100.0
        )

    circuit_distance_pct: float | None = None

    if circuit_distances:
        circuit_distance_pct = min(circuit_distances)

        if circuit_distance_pct < settings.min_circuit_distance_pct:
            return None, "too_close_to_circuit"

    gap_pct = (
        ((open_price - previous_close) / previous_close) * 100.0
    )
    day_range_pct = (
        ((high_price - low_price) / previous_close) * 100.0
    )
    change_pct = (
        ((last_price - previous_close) / previous_close) * 100.0
    )

    # Score prioritises live traded value, then activity/movement,
    # while rewarding tighter spreads and usable visible depth.
    turnover_score = min(
        math.log10(max(turnover, 1.0)) / 9.0,
        1.0,
    ) * 45.0

    volume_score = min(
        math.log10(max(volume, 1)) / 7.0,
        1.0,
    ) * 15.0

    movement_measure = (
        abs(gap_pct)
        + day_range_pct
        + abs(change_pct)
    )

    movement_score = min(
        movement_measure / 5.0,
        1.0,
    ) * 25.0

    if spread_pct is None:
        spread_score = 0.0
    else:
        spread_score = max(
            0.0,
            1.0 - (spread_pct / settings.max_spread_pct),
        ) * 10.0

    visible_depth_value = (
        (bid_quantity + ask_quantity) * last_price
    )

    depth_score = min(
        math.log10(max(visible_depth_value, 1.0)) / 8.0,
        1.0,
    ) * 5.0

    score = (
        turnover_score
        + volume_score
        + movement_score
        + spread_score
        + depth_score
    )

    tick_size = positive_float(
        universe_row.get("tick_size"),
        0.05,
    )

    if tick_size <= 0:
        tick_size = 0.05

    open_low_difference = open_price - low_price

    open_low_tolerance = (
        tick_size
        * max(settings.open_low_tolerance_ticks, 0)
        + 1e-9
    )

    open_equals_low = (
        settings.enable_open_equals_low_priority
        and abs(open_low_difference) <= open_low_tolerance
    )

    candidate = {
        "symbol": symbol,
        "exchange": "NSE",
        "company_name": universe_row.get("company_name", ""),
        "industry": universe_row.get("industry", ""),
        "tick_size": round(tick_size, 6),
        "open_low_difference": round(
            open_low_difference,
            6,
        ),
        "open_equals_low": open_equals_low,
        "instrument_token": positive_int(
            quote.get("instrument_token")
        ),
        "last_price": round(last_price, 4),
        "volume": volume,
        "turnover": round(turnover, 2),
        "gap_pct": round(gap_pct, 4),
        "day_range_pct": round(day_range_pct, 4),
        "change_pct": round(change_pct, 4),
        "best_bid": round(best_bid, 4),
        "best_ask": round(best_ask, 4),
        "spread_pct": (
            round(spread_pct, 4)
            if spread_pct is not None
            else None
        ),
        "circuit_distance_pct": (
            round(circuit_distance_pct, 4)
            if circuit_distance_pct is not None
            else None
        ),
        "score": round(score, 6),
    }

    return candidate, None


def _normalise_daily_candles(
    candles: Any,
) -> list[dict[str, Any]]:
    if not isinstance(candles, list):
        return []

    usable = [
        candle
        for candle in candles
        if isinstance(candle, dict)
        and positive_float(candle.get("close")) > 0
    ]

    usable.sort(
        key=lambda candle: str(candle.get("date") or "")
    )

    return usable


def calculate_previous_day_momentum(
    candles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    usable = _normalise_daily_candles(candles)

    if len(usable) < 2:
        return None

    previous_candle = usable[-2]
    latest_candle = usable[-1]

    previous_close = positive_float(
        previous_candle.get("close")
    )
    latest_close = positive_float(
        latest_candle.get("close")
    )
    latest_high = positive_float(
        latest_candle.get("high")
    )
    latest_low = positive_float(
        latest_candle.get("low")
    )
    latest_volume = positive_int(
        latest_candle.get("volume")
    )

    if (
        previous_close <= 0
        or latest_close <= 0
        or latest_high <= 0
        or latest_low <= 0
    ):
        return None

    return_pct = (
        (latest_close - previous_close)
        / previous_close
        * 100.0
    )

    range_pct = (
        (latest_high - latest_low)
        / previous_close
        * 100.0
    )

    baseline_volumes = [
        positive_int(candle.get("volume"))
        for candle in usable[:-1][-20:]
        if positive_int(candle.get("volume")) > 0
    ]

    if baseline_volumes:
        average_volume = (
            sum(baseline_volumes)
            / len(baseline_volumes)
        )
    else:
        average_volume = 0.0

    volume_ratio = (
        latest_volume / average_volume
        if latest_volume > 0 and average_volume > 0
        else 0.0
    )

    # Weighted fallback momentum score:
    # 60% absolute close-to-close return
    # 25% completed-day range
    # 15% relative volume, capped to avoid outliers dominating
    momentum_score = (
        0.60 * abs(return_pct)
        + 0.25 * range_pct
        + 0.15 * min(volume_ratio, 5.0)
    )

    if return_pct > 0:
        direction = "UP"
    elif return_pct < 0:
        direction = "DOWN"
    else:
        direction = "FLAT"

    return {
        "previous_day_date": str(
            latest_candle.get("date") or ""
        ),
        "previous_day_return_pct": round(
            return_pct,
            6,
        ),
        "previous_day_range_pct": round(
            range_pct,
            6,
        ),
        "previous_day_volume": latest_volume,
        "previous_day_average_volume": round(
            average_volume,
            2,
        ),
        "previous_day_volume_ratio": round(
            volume_ratio,
            6,
        ),
        "previous_day_direction": direction,
        "previous_day_momentum_score": round(
            momentum_score,
            6,
        ),
    }


def fetch_previous_day_momentum(
    kite: Any,
    matched_rows: list[dict[str, Any]],
    settings: SelectorSettings,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, int],
]:
    today = datetime.now(IST).date()

    from_date = today - timedelta(
        days=settings.historical_lookback_days
    )
    to_date = today - timedelta(days=1)

    momentum: dict[str, dict[str, Any]] = {}

    statistics = {
        "historical_requested": 0,
        "historical_received": 0,
        "historical_usable": 0,
        "historical_failed": 0,
    }

    consecutive_failures = 0
    total = len(matched_rows)

    for index, row in enumerate(matched_rows, 1):
        symbol = row["symbol"]
        instrument_token = positive_int(
            row.get("instrument_token")
        )

        if instrument_token <= 0:
            statistics["historical_failed"] += 1
            continue

        statistics["historical_requested"] += 1

        try:
            candles = kite.historical_data(
                instrument_token,
                from_date,
                to_date,
                "day",
                continuous=False,
                oi=False,
            )

            statistics["historical_received"] += 1
            consecutive_failures = 0

        except Exception as exc:
            statistics["historical_failed"] += 1
            consecutive_failures += 1

            print(
                "Historical data failed:",
                symbol,
                type(exc).__name__,
            )

            if consecutive_failures >= 10:
                raise AutoWatchlistError(
                    "Ten consecutive historical-data "
                    "requests failed. Aborting fallback "
                    "generation, likely because authentication "
                    "or the Kite API is unavailable."
                ) from exc

            time.sleep(
                settings.historical_delay_seconds
            )
            continue

        calculated = calculate_previous_day_momentum(
            candles
        )

        if calculated is not None:
            momentum[symbol] = calculated
            statistics["historical_usable"] += 1

        if index % 50 == 0 or index == total:
            print(
                "Previous-day momentum progress:",
                f"{index}/{total}",
            )

        if index < total:
            time.sleep(
                settings.historical_delay_seconds
            )

    return momentum, statistics


def generate_selection(
    kite: Any,
    universe: list[dict[str, str]],
    settings: SelectorSettings,
    *,
    allow_missing_depth: bool = False,
) -> dict[str, Any]:
    raw_instruments = kite.instruments("NSE")

    if not isinstance(raw_instruments, list):
        raise AutoWatchlistError(
            "Kite instrument response was not a list"
        )

    instrument_map = usable_nse_equity_instruments(
        raw_instruments
    )

    matched_rows: list[dict[str, Any]] = []

    for row in universe:
        instrument = instrument_map.get(row["symbol"])

        if instrument is None:
            continue

        matched_rows.append(
            {
                **row,
                "instrument_token": positive_int(
                    instrument.get("instrument_token")
                ),
                "tick_size": positive_float(
                    instrument.get("tick_size"),
                    0.05,
                ),
            }
        )

    if len(matched_rows) < settings.min_selected:
        raise AutoWatchlistError(
            "Too few NIFTY 500 symbols matched current "
            f"Kite instruments: {len(matched_rows)}"
        )

    quote_keys = [
        f"NSE:{row['symbol']}"
        for row in matched_rows
    ]

    # This is the single 9:16 live snapshot. Historical fallback
    # requests happen only after this quote snapshot is captured.
    quotes = fetch_full_quotes(kite, quote_keys)

    strict_eligible: list[dict[str, Any]] = []
    strict_rejections: Counter[str] = Counter()

    for row in matched_rows:
        quote_key = f"NSE:{row['symbol']}"
        quote = quotes.get(quote_key)

        if not isinstance(quote, dict):
            strict_rejections["missing_quote"] += 1
            continue

        candidate, rejection_reason = evaluate_quote(
            row,
            quote,
            settings,
            allow_missing_depth=allow_missing_depth,
        )

        if candidate is None:
            strict_rejections[
                rejection_reason or "unknown_rejection"
            ] += 1
            continue

        strict_eligible.append(candidate)

    def live_sort_key(
        item: dict[str, Any],
    ) -> tuple[Any, ...]:
        return (
            -positive_float(item.get("score")),
            -positive_float(item.get("turnover")),
            item["symbol"],
        )

    # Priority 1: Open = Low.
    priority_1 = [
        candidate
        for candidate in strict_eligible
        if candidate.get("open_equals_low") is True
    ]

    priority_1.sort(key=live_sort_key)

    for candidate in priority_1:
        candidate["selection_priority"] = (
            "PRIORITY_1_OPEN_EQUALS_LOW"
        )

    # Priority 2: strong current momentum, excluding Priority 1.
    priority_2 = [
        candidate
        for candidate in strict_eligible
        if candidate.get("open_equals_low") is not True
        and max(
            abs(
                positive_float(
                    candidate.get("change_pct")
                )
            ),
            positive_float(
                candidate.get("day_range_pct")
            ),
        )
        >= settings.min_live_momentum_pct
    ]

    priority_2.sort(key=live_sort_key)

    for candidate in priority_2:
        candidate["selection_priority"] = (
            "PRIORITY_2_LIVE_MOMENTUM"
        )

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

    # Priority 3: fill remaining spaces from the previous
    # completed trading day's strongest absolute momentum.
    if (
        len(selected) < settings.top_n
        and settings.enable_previous_day_momentum_fallback
    ):
        (
            momentum_by_symbol,
            historical_statistics,
        ) = fetch_previous_day_momentum(
            kite,
            matched_rows,
            settings,
        )

        # Previous-day fallback ignores the strict early-turnover
        # threshold, but still requires a live traded quote,
        # valid spread, acceptable price and circuit safety.
        fallback_settings = replace(
            settings,
            min_turnover=0.0,
        )

        for row in matched_rows:
            symbol = row["symbol"]

            if symbol in selected_symbols:
                continue

            momentum = momentum_by_symbol.get(symbol)

            if momentum is None:
                fallback_rejections[
                    "missing_previous_day_momentum"
                ] += 1
                continue

            quote = quotes.get(f"NSE:{symbol}")

            if not isinstance(quote, dict):
                fallback_rejections[
                    "missing_current_quote"
                ] += 1
                continue

            candidate, rejection_reason = evaluate_quote(
                row,
                quote,
                fallback_settings,
                allow_missing_depth=allow_missing_depth,
            )

            if candidate is None:
                fallback_rejections[
                    rejection_reason
                    or "unknown_fallback_rejection"
                ] += 1
                continue

            candidate.update(momentum)
            candidate["selection_priority"] = (
                "PRIORITY_3_PREVIOUS_DAY_MOMENTUM"
            )
            candidate["live_score"] = candidate["score"]
            candidate["score"] = momentum[
                "previous_day_momentum_score"
            ]

            priority_3.append(candidate)

        priority_3.sort(
            key=lambda item: (
                -positive_float(
                    item.get(
                        "previous_day_momentum_score"
                    )
                ),
                -abs(
                    positive_float(
                        item.get(
                            "previous_day_return_pct"
                        )
                    )
                ),
                -positive_float(
                    item.get(
                        "previous_day_volume_ratio"
                    )
                ),
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
        item.get("selection_priority", "UNKNOWN")
        for item in selected
    )

    status = (
        "success"
        if len(selected) >= settings.min_selected
        else "failed"
    )

    result: dict[str, Any] = {
        "status": status,
        "generated_at": now_ist_iso(),
        "source": {
            "universe": (
                "NIFTY 500 official constituent CSV"
            ),
            "universe_url": NIFTY_500_URL,
            "quotes": (
                "Kite Connect full market quotes"
            ),
            "previous_day_momentum": (
                "Kite Connect daily historical candles"
            ),
        },
        "settings": asdict(settings),
        "statistics": {
            "nifty500_rows": len(universe),
            "kite_nse_equities": len(instrument_map),
            "matched_symbols": len(matched_rows),
            "quotes_received": len(quotes),
            "strict_eligible_symbols": len(
                strict_eligible
            ),
            "priority_1_candidates": len(priority_1),
            "priority_2_candidates": len(priority_2),
            "priority_3_candidates": len(priority_3),
            "selected_symbols": len(selected),
            "selected_priority_counts": dict(
                sorted(selected_priority_counts.items())
            ),
            "strict_rejection_reasons": dict(
                sorted(strict_rejections.items())
            ),
            "fallback_rejection_reasons": dict(
                sorted(fallback_rejections.items())
            ),
            **historical_statistics,
        },
        "selected": selected,
    }

    if status != "success":
        result["error"] = (
            "Selection produced only "
            f"{len(selected)} safe unique symbols; "
            f"minimum required is "
            f"{settings.min_selected}"
        )

    return result


def write_watchlist_to_config(
    config_path: Path,
    selected: list[dict[str, Any]],
    *,
    min_selected: int,
    runtime_dir: Path,
) -> Path:
    if len(selected) < min_selected:
        raise AutoWatchlistError(
            "Refusing to write an undersized watchlist: "
            f"{len(selected)} < {min_selected}"
        )

    config_path = Path(config_path)

    if not config_path.exists():
        raise AutoWatchlistError(
            f"Configuration file not found: {config_path}"
        )

    try:
        current_config = json.loads(
            config_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoWatchlistError(
            f"Unable to read current configuration: {exc}"
        ) from exc

    if not isinstance(current_config, dict):
        raise AutoWatchlistError(
            "Current user configuration is not a JSON object"
        )

    watchlist = [
        {
            "symbol": item["symbol"],
            "exchange": "NSE",
        }
        for item in selected
    ]

    if not watchlist:
        raise AutoWatchlistError(
            "Refusing to write an empty watchlist"
        )

    runtime_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = runtime_dir / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    backup_path = (
        backup_dir
        / f"user_config-before-auto-watchlist-{timestamp}.json"
    )

    shutil.copy2(config_path, backup_path)

    updated_config = dict(current_config)
    updated_config["watchlist"] = watchlist

    atomic_write_json(config_path, updated_config)

    return backup_path


def print_summary(result: dict[str, Any]) -> None:
    statistics = result.get("statistics") or {}
    selected = result.get("selected") or []

    print()
    print("===== AUTO-WATCHLIST RESULT =====")
    print("Status:", result.get("status"))
    print("Generated at:", result.get("generated_at"))
    print(
        "NIFTY 500 rows:",
        statistics.get("nifty500_rows"),
    )
    print(
        "Matched symbols:",
        statistics.get("matched_symbols"),
    )
    print(
        "Quotes received:",
        statistics.get("quotes_received"),
    )
    print(
        "Strict eligible:",
        statistics.get("strict_eligible_symbols"),
    )
    print(
        "Priority 1 candidates:",
        statistics.get("priority_1_candidates"),
    )
    print(
        "Priority 2 candidates:",
        statistics.get("priority_2_candidates"),
    )
    print(
        "Priority 3 candidates:",
        statistics.get("priority_3_candidates"),
    )
    print(
        "Selected symbols:",
        statistics.get("selected_symbols"),
    )
    print(
        "Selected priority counts:",
        statistics.get("selected_priority_counts"),
    )

    print()
    print("Top selected symbols:")

    for item in selected[:80]:
        print(
            f"  {item['symbol']:<14} "
            f"{item.get('selection_priority', 'UNKNOWN'):<38} "
            f"score={positive_float(item.get('score')):>8.3f} "
            f"change={positive_float(item.get('change_pct')):>7.3f}% "
            f"range={positive_float(item.get('day_range_pct')):>7.3f}% "
            f"prev={positive_float(item.get('previous_day_return_pct')):>7.3f}%"
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an automatic NSE morning watchlist"
        )
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Atomically update user_config.json "
            "after validation"
        ),
    )
    parser.add_argument("--top", type=int, default=80)
    parser.add_argument(
        "--min-selected",
        type=int,
        default=80,
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=20.0,
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=5000.0,
    )
    parser.add_argument(
        "--min-turnover",
        type=float,
        default=500_000.0,
    )
    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=0.40,
    )
    parser.add_argument(
        "--min-circuit-distance-pct",
        type=float,
        default=0.75,
    )
    parser.add_argument(
        "--open-low-tolerance-ticks",
        type=int,
        default=0,
        help=(
            "Number of instrument ticks permitted between "
            "today's open and low. Zero means strict Open = Low."
        ),
    )
    parser.add_argument(
        "--min-live-momentum-pct",
        type=float,
        default=0.20,
        help=(
            "Minimum absolute current change or day range "
            "for Priority 2."
        ),
    )
    parser.add_argument(
        "--disable-open-equals-low-priority",
        action="store_true",
    )
    parser.add_argument(
        "--disable-previous-day-fallback",
        action="store_true",
    )
    parser.add_argument(
        "--historical-lookback-days",
        type=int,
        default=45,
    )
    parser.add_argument(
        "--historical-delay-seconds",
        type=float,
        default=0.36,
        help=(
            "Delay between historical requests to remain "
            "within the Kite historical-data rate limit."
        ),
    )
    parser.add_argument(
        "--allow-missing-depth",
        action="store_true",
        help=(
            "Allow missing bid/ask depth for an "
            "after-hours dry run. This cannot be "
            "combined with --write."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if args.write and args.allow_missing_depth:
        raise SystemExit(
            "ERROR: --allow-missing-depth cannot be combined with --write"
        )

    if args.top <= 0:
        raise SystemExit("ERROR: --top must be greater than zero")

    if args.min_selected <= 0:
        raise SystemExit(
            "ERROR: --min-selected must be greater than zero"
        )

    if args.min_selected > args.top:
        raise SystemExit(
            "ERROR: --min-selected cannot exceed --top"
        )

    if args.open_low_tolerance_ticks < 0:
        raise SystemExit(
            "ERROR: --open-low-tolerance-ticks "
            "cannot be negative"
        )

    if args.min_live_momentum_pct < 0:
        raise SystemExit(
            "ERROR: --min-live-momentum-pct "
            "cannot be negative"
        )

    if args.historical_lookback_days < 10:
        raise SystemExit(
            "ERROR: --historical-lookback-days "
            "must be at least 10"
        )

    if args.historical_delay_seconds < 0.34:
        raise SystemExit(
            "ERROR: --historical-delay-seconds "
            "must be at least 0.34"
        )

    settings = SelectorSettings(
        top_n=args.top,
        min_selected=args.min_selected,
        min_price=args.min_price,
        max_price=args.max_price,
        min_turnover=args.min_turnover,
        max_spread_pct=args.max_spread_pct,
        min_circuit_distance_pct=(
            args.min_circuit_distance_pct
        ),
        enable_open_equals_low_priority=(
            not args.disable_open_equals_low_priority
        ),
        open_low_tolerance_ticks=(
            args.open_low_tolerance_ticks
        ),
        min_live_momentum_pct=(
            args.min_live_momentum_pct
        ),
        enable_previous_day_momentum_fallback=(
            not args.disable_previous_day_fallback
        ),
        historical_lookback_days=(
            args.historical_lookback_days
        ),
        historical_delay_seconds=(
            args.historical_delay_seconds
        ),
    )

    try:
        universe = download_nifty500()
        kite = get_kite_client()

        result = generate_selection(
            kite,
            universe,
            settings,
            allow_missing_depth=args.allow_missing_depth,
        )

        atomic_write_json(args.report, result)
        print_summary(result)

        if result["status"] != "success":
            print()
            print("FAIL:", result.get("error"))
            print("Configuration was not changed.")
            return 2

        watchlist_payload = {
            "status": "success",
            "generated_at": result["generated_at"],
            "watchlist": [
                {
                    "symbol": item["symbol"],
                    "exchange": "NSE",
                }
                for item in result["selected"]
            ],
            "selected_details": result["selected"],
        }

        atomic_write_json(args.output, watchlist_payload)

        if args.write:
            backup_path = write_watchlist_to_config(
                args.config,
                result["selected"],
                min_selected=settings.min_selected,
                runtime_dir=args.report.parent,
            )

            print()
            print(
                "Configuration updated atomically:",
                args.config,
            )
            print("Configuration backup:", backup_path)
        else:
            print()
            print(
                "DRY RUN: user_config.json was not modified."
            )

        return 0

    except Exception as exc:
        failure_report = {
            "status": "failed",
            "generated_at": now_ist_iso(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "configuration_changed": False,
        }

        try:
            atomic_write_json(args.report, failure_report)
        except Exception:
            pass

        print()
        print("AUTO-WATCHLIST FAILED")
        print("Error type:", type(exc).__name__)
        print("Error:", exc)
        print("Configuration was not changed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
