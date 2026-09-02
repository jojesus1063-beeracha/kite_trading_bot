#!/usr/bin/env python3
"""Read-only Kite report: last week's minute-level high/low for the paper top-60."""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_WATCHLIST = "runtime/historical_all_nse_watchlist_20260814_0927.json"
DEFAULT_FROM = "2026-08-10 09:15:00"
DEFAULT_TO = "2026-08-14 15:30:00"


def get_client():
    attempts: list[str] = []
    for module_name in (
        "auth",
        "kite_auth",
        "auth_helper",
        "isolated_current_paper_workflow_replay",
        "isolated_vwap_ema_replay",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # keep trying the project's known auth entry points
            attempts.append(f"{module_name}: import failed ({exc})")
            continue
        for function_name in ("get_kite_client", "create_kite_client", "get_client"):
            function = getattr(module, function_name, None)
            if callable(function) and function is not get_client:
                try:
                    return function()
                except Exception as exc:
                    attempts.append(f"{module_name}.{function_name}: {exc}")
    raise RuntimeError(
        "Could not create the authenticated Kite client. Refresh today's access token "
        "and, if needed, update get_client() with the auth module used by this repo.\n"
        + "\n".join(attempts)
    )


def symbol_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip().removeprefix("NSE:") or None
    if isinstance(item, dict):
        for key in ("symbol", "tradingsymbol", "ticker"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().removeprefix("NSE:")
    return None


def load_symbols(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    candidates: list[Any] = []

    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("selected", "symbols", "watchlist", "stocks"):
            value = data.get(key)
            if isinstance(value, list) and value:
                candidates = value
                break
        if not candidates and isinstance(data.get("qualified"), list):
            candidates = [
                row for row in data["qualified"]
                if isinstance(row, dict) and row.get("selected", True)
            ]

    symbols: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        symbol = symbol_from_item(item)
        if symbol and symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)

    if not symbols:
        raise ValueError(f"No symbols found in {path}")
    return symbols


def iso_ist(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def extreme_times(candles: list[dict[str, Any]], field: str, value: float) -> list[str]:
    return [iso_ist(row["date"]) for row in candles if float(row[field]) == value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    parser.add_argument("--from-date", default=DEFAULT_FROM)
    parser.add_argument("--to-date", default=DEFAULT_TO)
    parser.add_argument("--interval", default="minute")
    parser.add_argument("--csv", default="runtime/watchlist_extremes_20260810_20260814.csv")
    parser.add_argument("--json", default="runtime/watchlist_extremes_20260810_20260814.json")
    args = parser.parse_args()

    watchlist_path = Path(args.watchlist)
    symbols = load_symbols(watchlist_path)
    start = datetime.fromisoformat(args.from_date)
    end = datetime.fromisoformat(args.to_date)

    print(f"WATCHLIST={watchlist_path}")
    print(f"SYMBOLS={len(symbols)}")
    print(f"WINDOW_IST={start} to {end}")
    print(f"INTERVAL={args.interval}")

    kite = get_client()
    instruments = kite.instruments("NSE")
    token_by_symbol = {
        row["tradingsymbol"]: int(row["instrument_token"])
        for row in instruments
        if row.get("tradingsymbol") and row.get("instrument_type") == "EQ"
    }

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for number, symbol in enumerate(symbols, 1):
        token = token_by_symbol.get(symbol)
        if token is None:
            failures.append({"symbol": symbol, "error": "EQ instrument token not found"})
            print(f"SKIP {number}/{len(symbols)} {symbol}: token not found")
            continue
        try:
            candles = kite.historical_data(token, start, end, args.interval)
        except Exception as exc:
            failures.append({"symbol": symbol, "error": str(exc)})
            print(f"FAIL {number}/{len(symbols)} {symbol}: {exc}")
            continue
        if not candles:
            failures.append({"symbol": symbol, "error": "no candles returned"})
            print(f"SKIP {number}/{len(symbols)} {symbol}: no candles")
            continue

        weekly_high = max(float(row["high"]) for row in candles)
        weekly_low = min(float(row["low"]) for row in candles)
        high_times = extreme_times(candles, "high", weekly_high)
        low_times = extreme_times(candles, "low", weekly_low)
        range_pct = ((weekly_high / weekly_low) - 1.0) * 100.0 if weekly_low else None
        result = {
            "symbol": symbol,
            "weekly_high": weekly_high,
            "high_first_time_ist": high_times[0],
            "high_last_time_ist": high_times[-1],
            "weekly_low": weekly_low,
            "low_first_time_ist": low_times[0],
            "low_last_time_ist": low_times[-1],
            "high_low_range_pct": round(range_pct, 4) if range_pct is not None else None,
            "candles": len(candles),
        }
        rows.append(result)
        print(
            f"OK {number}/{len(symbols)} {symbol} "
            f"HIGH={weekly_high:g} @ {high_times[0]} "
            f"LOW={weekly_low:g} @ {low_times[0]} "
            f"RANGE={range_pct:.2f}%"
        )
        time.sleep(0.35)

    widest = max(rows, key=lambda row: row["high_low_range_pct"]) if rows else None
    payload = {
        "methodology": {
            "watchlist": str(watchlist_path),
            "window_start_ist": args.from_date,
            "window_end_ist": args.to_date,
            "interval": args.interval,
            "source": "Kite Connect historical_data; read-only",
            "tie_policy": "first and last candle timestamps are both retained",
        },
        "summary": {
            "watchlist_symbols": len(symbols),
            "completed": len(rows),
            "failures": len(failures),
            "widest_range_symbol": widest["symbol"] if widest else None,
            "widest_range_pct": widest["high_low_range_pct"] if widest else None,
        },
        "stocks": rows,
        "failures": failures,
    }

    csv_path = Path(args.csv)
    json_path = Path(args.json)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with csv_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    print(json.dumps(payload["summary"], indent=2))
    print(f"CSV={csv_path}")
    print(f"JSON={json_path}")
    if failures:
        print("WARNING: some symbols failed; inspect the JSON failures list", file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
