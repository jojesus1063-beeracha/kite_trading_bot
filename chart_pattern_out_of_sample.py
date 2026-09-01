#!/usr/bin/env python3
"""Out-of-sample validation of the four surviving chart patterns.

Uses each session's genuinely frozen 09:27 watchlist. The 2026-08-10 through
2026-08-14 optimization window is excluded by default. Read-only: no orders or
broker state writes.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

import pandas as pd

from auth import get_kite_client
from chart_pattern_replay import (
    IST,
    discover_signals,
    fetch_frame,
    load_watchlist,
    trade_costs,
)


PATTERN_TARGETS = {
    "RISING_WEDGE": 2.00,
    "BEARISH_WEDGE_CHANNEL": 2.00,
    "TRIPLE_TOP": 1.00,
    "TRIPLE_BOTTOM": 2.00,
}
STOP_PCT = 0.45


def discover_daily_reports(pattern: str, exclude_from, exclude_to):
    reports = []
    rx = re.compile(r"historical_all_nse_watchlist_(\d{8})_0927\.json$")
    for path in sorted(Path().glob(pattern)):
        match = rx.search(path.name)
        if not match:
            continue
        day = pd.Timestamp(match.group(1)).date()
        if exclude_from <= day <= exclude_to:
            continue
        reports.append((day, path))
    return reports


def simulate(frame, signal, capital):
    after = frame.loc[
        (frame["date"] > signal.signal_time)
        & (frame["date"].dt.date == signal.signal_time.date())
        & (frame["date"].dt.time <= clock_time(15, 9))
    ].copy()
    if after.empty:
        return None
    entry_time = after.iloc[0]["date"]
    entry = float(after.iloc[0]["open"])
    direction = signal.direction
    sign = 1 if direction == "BUY" else -1
    target_pct = PATTERN_TARGETS[signal.pattern]
    stop = entry * (1 - sign * STOP_PCT / 100)
    target = entry * (1 + sign * target_pct / 100)
    risk_per_share = entry * STOP_PCT / 100
    qty = int((capital * 0.002) / risk_per_share)
    if qty <= 0:
        return None
    exit_price = float(after.iloc[-1]["close"])
    exit_time = after.iloc[-1]["date"]
    reason = "EOD"
    for _, bar in after.iterrows():
        stop_hit = float(bar["low"]) <= stop if direction == "BUY" else float(bar["high"]) >= stop
        target_hit = float(bar["high"]) >= target if direction == "BUY" else float(bar["low"]) <= target
        if stop_hit:
            exit_price, exit_time, reason = stop, bar["date"], "STOP"
            break
        if target_hit:
            exit_price, exit_time, reason = target, bar["date"], "TARGET"
            break
    pnl = trade_costs(direction, qty, entry, exit_price)
    return {
        "date": signal.signal_time.date().isoformat(),
        "symbol": signal.symbol,
        "pattern": signal.pattern,
        "direction": direction,
        "signal_time": signal.signal_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry": entry,
        "stop": stop,
        "target": target,
        "target_pct": target_pct,
        "qty": qty,
        "exit": exit_price,
        "exit_reason": reason,
        "volume_ratio": signal.volume_ratio,
        "vwap_aligned": signal.vwap_aligned,
        **{key: float(value) for key, value in pnl.items()},
    }


def admit(signals, frames, capital, confirmed):
    rows = []
    open_positions = []
    traded_symbol_days = set()
    daily_count = Counter()
    rejections = Counter()
    for signal in sorted(signals, key=lambda item: (item.signal_time, item.symbol, item.pattern)):
        day = signal.signal_time.date().isoformat()
        if signal.pattern not in PATTERN_TARGETS:
            continue
        if confirmed and not (
            signal.vwap_aligned is True
            and signal.volume_ratio is not None
            and signal.volume_ratio >= 1.5
        ):
            rejections["CONFIRMATION"] += 1
            continue
        symbol_day = (day, signal.symbol)
        if symbol_day in traded_symbol_days:
            rejections["ONE_TRADE_PER_SYMBOL_DAY"] += 1
            continue
        now = signal.signal_time + pd.Timedelta(minutes=3)
        open_positions = [trade for trade in open_positions if pd.Timestamp(trade["exit_time"]) > now]
        if len(open_positions) >= 5:
            rejections["MAX_OPEN_POSITIONS"] += 1
            continue
        if daily_count[day] >= 100:
            rejections["MAX_TRADES_DAY"] += 1
            continue
        trade = simulate(frames[(day, signal.symbol)], signal, capital)
        if trade is None:
            rejections["NO_SIZE_OR_EXIT"] += 1
            continue
        rows.append(trade)
        open_positions.append(trade)
        traded_symbol_days.add(symbol_day)
        daily_count[day] += 1
    return rows, dict(rejections)


def stats(rows):
    wins = sum(row["net_pnl"] > 0 for row in rows)
    losses = sum(row["net_pnl"] < 0 for row in rows)
    gains = sum(max(row["net_pnl"], 0) for row in rows)
    pains = abs(sum(min(row["net_pnl"], 0) for row in rows))
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) * 100 if rows else 0.0,
        "gross_pnl": sum(row["gross_pnl"] for row in rows),
        "costs": sum(row["costs"] for row in rows),
        "net_pnl": sum(row["net_pnl"] for row in rows),
        "profit_factor": gains / pains if pains else None,
    }


def summaries(rows):
    result = {"overall": stats(rows), "by_pattern": {}, "by_day": {}}
    for pattern in sorted({row["pattern"] for row in rows}):
        result["by_pattern"][pattern] = stats([row for row in rows if row["pattern"] == pattern])
    for day in sorted({row["date"] for row in rows}):
        result["by_day"][day] = stats([row for row in rows if row["date"] == day])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist-glob", default="runtime/historical_all_nse_watchlist_*_0927.json")
    parser.add_argument("--exclude-from", default="2026-08-10")
    parser.add_argument("--exclude-to", default="2026-08-14")
    parser.add_argument("--capital", type=float, default=5000.0)
    parser.add_argument("--output", default="runtime/chart_pattern_out_of_sample.json")
    parser.add_argument("--csv", default="runtime/chart_pattern_out_of_sample.csv")
    args = parser.parse_args()

    exclude_from = pd.Timestamp(args.exclude_from).date()
    exclude_to = pd.Timestamp(args.exclude_to).date()
    reports = discover_daily_reports(args.watchlist_glob, exclude_from, exclude_to)
    if not reports:
        raise SystemExit("No out-of-sample daily frozen watchlist reports found")

    print("READ_ONLY_REPLAY=True")
    print("UNIVERSE=GENUINE_DAILY_FROZEN_0927_WATCHLISTS")
    print(f"OPTIMIZATION_WINDOW_EXCLUDED={exclude_from}..{exclude_to}")
    print("PATTERNS=" + ",".join(PATTERN_TARGETS))
    print(f"DATES={','.join(str(day) for day, _ in reports)}")

    kite = get_kite_client()
    tokens = {
        str(item.get("tradingsymbol")): int(item["instrument_token"])
        for item in kite.instruments("NSE")
        if item.get("instrument_type") == "EQ"
    }
    all_signals, frames, failures = [], {}, {}
    total_fetches = sum(len(load_watchlist(path)) for _, path in reports)
    fetch_number = 0
    for day, report_path in reports:
        symbols = load_watchlist(report_path)
        start = datetime.combine(day - timedelta(days=10), clock_time(9, 15), IST)
        end = datetime.combine(day, clock_time(15, 30), IST)
        for symbol in symbols:
            fetch_number += 1
            token = tokens.get(symbol)
            if token is None:
                failures[f"{day}:{symbol}"] = "TOKEN_NOT_FOUND"
                continue
            try:
                frame = fetch_frame(kite, token, start, end)
                frames[(day.isoformat(), symbol)] = frame
                found = discover_signals(symbol, frame, day, day)
                selected = [signal for signal in found if signal.pattern in PATTERN_TARGETS]
                all_signals.extend(selected)
                print(
                    f"FETCH {fetch_number}/{total_fetches} {day} NSE:{symbol} "
                    f"candles={len(frame)} selected_signals={len(selected)}"
                )
                time.sleep(0.35)
            except Exception as exc:
                failures[f"{day}:{symbol}"] = str(exc)

    raw, raw_rejections = admit(all_signals, frames, args.capital, confirmed=False)
    confirmed, confirmed_rejections = admit(all_signals, frames, args.capital, confirmed=True)
    raw_summary = summaries(raw)
    confirmed_summary = summaries(confirmed)
    report = {
        "method": "backward holdout; genuine daily frozen 09:27 watchlists; next 3-minute open; stop-first",
        "excluded_optimization_window": [args.exclude_from, args.exclude_to],
        "dates": [str(day) for day, _ in reports],
        "pattern_targets": PATTERN_TARGETS,
        "stop_pct": STOP_PCT,
        "risk_per_trade_pct": 0.2,
        "max_open_positions": 5,
        "one_trade_per_symbol_day": True,
        "signals": len(all_signals),
        "fetch_failures": failures,
        "raw": {"summary": raw_summary, "rejections": raw_rejections, "trades": raw},
        "confirmed": {
            "rule": "VWAP aligned and volume ratio >= 1.5",
            "summary": confirmed_summary,
            "rejections": confirmed_rejections,
            "trades": confirmed,
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    flat = [{"variant": "raw", **row} for row in raw]
    flat += [{"variant": "confirmed", **row} for row in confirmed]
    pd.DataFrame(flat).to_csv(args.csv, index=False)

    print("\nOUT-OF-SAMPLE PATTERN VALIDATION")
    print(f"DATES={len(reports)} SIGNALS={len(all_signals)} FETCH_FAILURES={len(failures)}")
    for label, summary in (("RAW", raw_summary), ("CONFIRMED", confirmed_summary)):
        item = summary["overall"]
        print(
            f"{label:10s} trades={item['trades']:3d} wins={item['wins']:3d} "
            f"losses={item['losses']:3d} win_rate={item['win_rate']:6.2f}% "
            f"gross=Rs {item['gross_pnl']:+.2f} costs=Rs {item['costs']:.2f} "
            f"net=Rs {item['net_pnl']:+.2f} PF={item['profit_factor']}"
        )
        for pattern, values in summary["by_pattern"].items():
            print(
                f"  {pattern:32s} n={values['trades']:3d} "
                f"win_rate={values['win_rate']:6.2f}% net=Rs {values['net_pnl']:+.2f}"
            )
    print(f"\nJSON={args.output}")
    print(f"CSV={args.csv}")


if __name__ == "__main__":
    main()
