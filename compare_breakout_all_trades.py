#!/usr/bin/env python3
"""Read-only breakout impact analysis for every retained historical entry.

This is an historical-entry-cohort comparison, not a full-universe backtest.
It answers which trades the new hard breakout gate would have retained or
rejected; it cannot discover trades the older watchlists never selected.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from breakout_validator import validate_breakout


IST = "Asia/Kolkata"
ENTRY_MINUTES = 3


@dataclass
class HistoricalTrade:
    key: str
    date: str
    symbol: str
    exchange: str
    direction: str
    timestamp: pd.Timestamp
    entry: float
    gross_pnl: float
    costs: float
    net_pnl: float
    results: str
    legs: int


def ist_timestamp(value, date_hint=None) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if date_hint and len(text) <= 8:
            text = f"{date_hint} {text}"
        stamp = pd.Timestamp(text)
        if stamp.tzinfo is None:
            return stamp.tz_localize(IST)
        return stamp.tz_convert(IST)
    except Exception:
        return None


def normalized_candle_start(stamp: pd.Timestamp) -> pd.Timestamp:
    session = stamp.normalize() + pd.Timedelta(hours=9, minutes=15)
    elapsed = max(0, int((stamp - session).total_seconds() // 60))
    return session + pd.Timedelta(
        minutes=(elapsed // ENTRY_MINUTES) * ENTRY_MINUTES
    )


def number(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_trades(path: Path) -> list[HistoricalTrade]:
    groups: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, 1):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            symbol = str(row.get("symbol") or "").strip()
            date = str(row.get("date") or "").strip()
            if not symbol or not date or row.get("entry") is None:
                continue
            key = str(row.get("signal_id") or "").strip()
            if not key:
                key = "fallback:{symbol}|{direction}|{entry}|{entry_time}".format(
                    symbol=symbol,
                    direction=row.get("direction"),
                    entry=row.get("entry"),
                    entry_time=(
                        row.get("entry_time")
                        or row.get("signal_candle_start")
                        or row.get("time")
                        or line_number
                    ),
                )
            groups[key].append(row)

    trades = []
    for key, legs in groups.items():
        first = legs[0]
        date = str(first.get("date"))
        stamp = (
            ist_timestamp(first.get("signal_candle_start"), date)
            or ist_timestamp(first.get("entry_time"), date)
            or ist_timestamp(first.get("time"), date)
        )
        if stamp is None:
            continue
        stamp = normalized_candle_start(stamp)
        results = sorted({
            str(row.get("result") or row.get("reason") or "UNKNOWN")
            for row in legs
        })
        trades.append(HistoricalTrade(
            key=key,
            date=date,
            symbol=str(first.get("symbol")),
            exchange=str(first.get("exchange") or "NSE"),
            direction=str(first.get("direction") or "").upper(),
            timestamp=stamp,
            entry=number(first.get("entry")),
            gross_pnl=sum(number(row.get("gross_pnl")) for row in legs),
            costs=sum(number(row.get("costs")) for row in legs),
            net_pnl=sum(number(row.get("pnl")) for row in legs),
            results="+".join(results),
            legs=len(legs),
        ))
    return sorted(trades, key=lambda row: (row.timestamp, row.symbol))


def instrument_map(kite) -> dict[tuple[str, str], int]:
    result = {}
    for exchange in ("NSE", "BSE"):
        for item in kite.instruments(exchange):
            symbol = str(item.get("tradingsymbol") or "")
            if symbol:
                result[(exchange, symbol)] = int(item["instrument_token"])
    return result


def fetch_frame(kite, token, start, end, attempts=3) -> pd.DataFrame:
    error = None
    for attempt in range(attempts):
        try:
            data = kite.historical_data(token, start, end, "3minute")
            frame = pd.DataFrame(data)
            if frame.empty:
                return frame
            frame["date"] = pd.to_datetime(frame["date"])
            if frame["date"].dt.tz is None:
                frame["date"] = frame["date"].dt.tz_localize(IST)
            else:
                frame["date"] = frame["date"].dt.tz_convert(IST)
            return frame[[
                "date", "open", "high", "low", "close", "volume"
            ]].sort_values("date").reset_index(drop=True)
        except Exception as exc:
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(
        f"historical_data failed after {attempts} attempts: {error}"
    )


def evaluate_trade(trade: HistoricalTrade, frame: pd.DataFrame) -> dict:
    point_in_time = frame.loc[frame["date"] <= trade.timestamp].copy()
    if point_in_time.empty or point_in_time.iloc[-1]["date"] != trade.timestamp:
        return {
            **asdict(trade),
            "timestamp": trade.timestamp.isoformat(),
            "breakout_passed": None,
            "data_status": "MISSING_ENTRY_CANDLE",
            "reasons": "MISSING_ENTRY_CANDLE",
        }

    result = validate_breakout(
        point_in_time,
        trade.direction,
        lookback=20,
        volume_period=20,
        minimum_volume_ratio=1.5,
        atr_period=14,
        minimum_atr_multiplier=1.2,
        clv_threshold=0.60,
    )
    metrics = result.metrics
    return {
        **asdict(trade),
        "timestamp": trade.timestamp.isoformat(),
        "breakout_passed": result.passed,
        "data_status": "OK",
        "n_period_high": metrics.get("n_period_high"),
        "n_period_low": metrics.get("n_period_low"),
        "breakout_close": metrics.get("breakout_close"),
        "structure_confirmed": metrics.get("structure_confirmed"),
        "volume_ratio": metrics.get("volume_ratio"),
        "volume_confirmed": metrics.get("volume_confirmed"),
        "atr_multiplier": metrics.get("atr_multiplier"),
        "volatility_confirmed": metrics.get("volatility_confirmed"),
        "clv": metrics.get("clv"),
        "clv_confirmed": metrics.get("clv_confirmed"),
        "reasons": ",".join(result.reasons),
    }


def cohort_summary(rows: list[dict]) -> dict:
    wins = sum(number(row.get("net_pnl")) > 0 for row in rows)
    losses = sum(number(row.get("net_pnl")) < 0 for row in rows)
    net = sum(number(row.get("net_pnl")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) * 100.0 if rows else 0.0,
        "net_pnl": net,
    }


def print_summary(label: str, rows: list[dict]) -> None:
    summary = cohort_summary(rows)
    print(
        f"{label:<30} n={summary['trades']:3d} "
        f"wins={summary['wins']:3d} losses={summary['losses']:3d} "
        f"win_rate={summary['win_rate']:6.2f}% net=Rs {summary['net_pnl']:+.2f}"
    )


def main() -> None:
    # Keep broker dependencies out of module import so pure aggregation tests
    # remain runnable without KiteConnect installed.
    from auth import get_kite_client

    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default="trade_history.jsonl")
    parser.add_argument(
        "--output", default="runtime/breakout_impact_all_trades.json"
    )
    parser.add_argument(
        "--csv", default="runtime/breakout_impact_all_trades.csv"
    )
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    args = parser.parse_args()

    trades = load_trades(Path(args.history))
    if args.from_date:
        trades = [row for row in trades if row.date >= args.from_date]
    if args.to_date:
        trades = [row for row in trades if row.date <= args.to_date]
    if not trades:
        raise SystemExit("No completed historical entries found")

    print("READ_ONLY_REPLAY=True")
    print("COHORT=UNIQUE_RETAINED_COMPLETED_HISTORICAL_ENTRIES")
    print(f"TRADES={len(trades)} DATES={trades[0].date}..{trades[-1].date}")
    print("IMPORTANT=Not a full-universe discovery backtest")

    kite = get_kite_client()
    tokens = instrument_map(kite)
    by_symbol = defaultdict(list)
    for trade in trades:
        by_symbol[(trade.exchange, trade.symbol)].append(trade)

    frames = {}
    fetch_failures = {}
    for count, ((exchange, symbol), rows) in enumerate(
        sorted(by_symbol.items()), 1
    ):
        token = tokens.get((exchange, symbol))
        if token is None:
            fetch_failures[f"{exchange}:{symbol}"] = "TOKEN_NOT_FOUND"
            continue
        start = min(row.timestamp for row in rows) - pd.Timedelta(days=7)
        end = max(row.timestamp for row in rows) + pd.Timedelta(days=1)
        try:
            frame = fetch_frame(
                kite, token, start.to_pydatetime(), end.to_pydatetime()
            )
            frames[(exchange, symbol)] = frame
            print(
                f"FETCH {count}/{len(by_symbol)} {exchange}:{symbol} "
                f"candles={len(frame)}"
            )
            time.sleep(0.35)
        except Exception as exc:
            fetch_failures[f"{exchange}:{symbol}"] = str(exc)

    rows = []
    for trade in trades:
        frame = frames.get((trade.exchange, trade.symbol))
        if frame is None:
            rows.append({
                **asdict(trade),
                "timestamp": trade.timestamp.isoformat(),
                "breakout_passed": None,
                "data_status": "DATA_UNAVAILABLE",
                "reasons": "DATA_UNAVAILABLE",
            })
        else:
            rows.append(evaluate_trade(trade, frame))

    passed = [row for row in rows if row.get("breakout_passed") is True]
    rejected = [row for row in rows if row.get("breakout_passed") is False]
    unavailable = [row for row in rows if row.get("breakout_passed") is None]

    print("\n" + "=" * 118)
    print("HARD BREAKOUT GATE — ALL RETAINED HISTORICAL TRADES")
    print("=" * 118)
    print_summary("Actual completed cohort", rows)
    print_summary("Hard gate would KEEP", passed)
    print_summary("Hard gate would REJECT", rejected)
    print_summary("Data unavailable", unavailable)

    print("\nPER DAY")
    days = sorted({row["date"] for row in rows})
    for day in days:
        day_rows = [row for row in rows if row["date"] == day]
        day_passed = [
            row for row in day_rows if row.get("breakout_passed") is True
        ]
        actual = cohort_summary(day_rows)
        kept = cohort_summary(day_passed)
        print(
            f"{day} actual_n={actual['trades']:2d} actual_net=Rs {actual['net_pnl']:+8.2f} "
            f"kept_n={kept['trades']:2d} kept_wins={kept['wins']:2d} "
            f"kept_net=Rs {kept['net_pnl']:+8.2f}"
        )

    print("\nINDIVIDUAL BREAKOUT COMPONENTS")
    for field, label in (
        ("structure_confirmed", "N20 structure passed"),
        ("volume_confirmed", "Volume >= 1.50x"),
        ("volatility_confirmed", "ATR expansion >= 1.20x"),
        ("clv_confirmed", "Directional CLV passed"),
    ):
        component_rows = [row for row in rows if row.get(field) is True]
        print_summary(label, component_rows)

    reason_counts = Counter()
    for row in rejected:
        for reason in str(row.get("reasons") or "").split(","):
            if reason:
                reason_counts[reason] += 1
    print("\nREJECTION REASONS")
    for reason, count in reason_counts.most_common():
        print(f"{reason:45s} {count:4d}")

    output = Path(args.output)
    csv_output = Path(args.csv)
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "actual": cohort_summary(rows),
        "hard_gate_keep": cohort_summary(passed),
        "hard_gate_reject": cohort_summary(rejected),
        "data_unavailable": cohort_summary(unavailable),
    }
    output.write_text(json.dumps({
        "method": "point-in-time breakout replay on unique retained completed historical entries",
        "limitations": [
            "not a full-universe discovery backtest",
            "uses actual retained trade P&L and historical 3-minute entry candles",
            "partial exit legs are aggregated by signal_id when available",
        ],
        "summary": summary,
        "fetch_failures": fetch_failures,
        "trades": rows,
    }, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(rows).to_csv(csv_output, index=False)
    print(f"\nDETAIL={output}")
    print(f"TABLE={csv_output}")


if __name__ == "__main__":
    main()
