#!/usr/bin/env python3

import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auth import get_kite_client

IST = ZoneInfo("Asia/Kolkata")

HISTORY = Path("trade_history.jsonl")
CACHE = Path("runtime/swing_daily_cache")
OUTPUT = Path(
    "runtime/swing_no_stop_target2_all_days_20260816.json"
)

CAPITAL_PER_TRADE = 5000.0
TARGET_PCT = 2.0
MAX_FUTURE_SESSIONS = 5

CACHE.mkdir(parents=True, exist_ok=True)


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(row):
    value = (
        row.get("entry_time")
        or row.get("timestamp")
        or row.get("time")
    )

    if not value:
        return None

    text = str(value).strip()

    if len(text) <= 8 and row.get("date"):
        text = f"{row['date']}T{text}"

    try:
        stamp = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    else:
        stamp = stamp.astimezone(IST)

    return stamp


def load_entries():
    entries = []

    for line in HISTORY.read_text(
        encoding="utf-8"
    ).splitlines():
        if not line.strip():
            continue

        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        stamp = parse_time(row)
        symbol = str(
            row.get("symbol")
            or row.get("tradingsymbol")
            or ""
        ).strip().upper()

        direction = str(
            row.get("direction")
            or row.get("transaction_type")
            or ""
        ).strip().upper()

        entry = number(
            row.get("entry")
            or row.get("entry_price")
            or row.get("average_price")
        )

        if (
            stamp is None
            or not symbol
            or entry <= 0
            or direction not in {"BUY", "SELL"}
        ):
            continue

        entries.append({
            "timestamp": stamp,
            "exchange": str(
                row.get("exchange") or "NSE"
            ).upper(),
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
        })

    entries.sort(key=lambda row: row["timestamp"])

    unique = []
    seen = set()

    for row in entries:
        key = (
            row["timestamp"].isoformat(),
            row["exchange"],
            row["symbol"],
            row["direction"],
            round(row["entry"], 4),
        )

        if key not in seen:
            seen.add(key)
            unique.append(row)

    return unique


def token_map(kite):
    result = {}

    for exchange in ("NSE", "BSE"):
        for instrument in kite.instruments(exchange):
            symbol = str(
                instrument.get("tradingsymbol") or ""
            ).strip().upper()

            if symbol:
                result[(exchange, symbol)] = (
                    instrument["instrument_token"]
                )

    return result


def fetch_daily(
    kite,
    token,
    exchange,
    symbol,
    start_date,
    end_date,
):
    safe_symbol = symbol.replace("&", "_AND_")
    path = CACHE / f"{exchange}_{safe_symbol}.json"

    if path.exists():
        raw = json.loads(
            path.read_text(encoding="utf-8")
        )
    else:
        last_error = None

        for attempt in range(1, 4):
            try:
                raw = kite.historical_data(
                    token,
                    start_date,
                    end_date,
                    "day",
                )
                break
            except Exception as exc:
                last_error = exc
                print(
                    f"RETRY={attempt}/3 "
                    f"{exchange}:{symbol} error={exc}"
                )
                time.sleep(attempt * 2)
        else:
            raise RuntimeError(str(last_error))

        serializable = []

        for candle in raw:
            item = dict(candle)
            item["date"] = str(item["date"])
            serializable.append(item)

        path.write_text(
            json.dumps(serializable),
            encoding="utf-8",
        )
        raw = serializable
        time.sleep(0.36)

    candles = []

    for candle in raw:
        text = str(candle["date"])
        stamp = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        candles.append({
            "date": stamp.date(),
            "open": number(candle.get("open")),
            "high": number(candle.get("high")),
            "low": number(candle.get("low")),
            "close": number(candle.get("close")),
        })

    return sorted(
        candles,
        key=lambda candle: candle["date"],
    )


def delivery_costs(buy_value, sell_value):
    turnover = buy_value + sell_value

    stt = turnover * 0.001
    transaction = turnover * 0.0000307
    sebi = turnover * 0.000001
    gst = (transaction + sebi) * 0.18
    stamp = buy_value * 0.00015
    dp = 13.0 * 1.18

    return stt + transaction + sebi + gst + stamp + dp


def make_result(
    entry,
    scenario,
    exit_date=None,
    exit_price=None,
    reason="UNRESOLVED",
    completed=False,
):
    qty = math.floor(
        CAPITAL_PER_TRADE / entry["entry"]
    )

    row = {
        "scenario": scenario,
        "symbol": entry["symbol"],
        "entry_time": entry["timestamp"].isoformat(),
        "entry": entry["entry"],
        "qty": qty,
        "completed": completed,
        "exit_reason": reason,
    }

    if (
        not completed
        or exit_price is None
        or qty <= 0
    ):
        row.update({
            "exit_date": (
                str(exit_date) if exit_date else None
            ),
            "exit": exit_price,
            "gross_pnl": None,
            "costs": None,
            "net_pnl": None,
        })
        return row

    buy_value = qty * entry["entry"]
    sell_value = qty * exit_price
    gross = sell_value - buy_value
    costs = delivery_costs(buy_value, sell_value)

    row.update({
        "exit_date": str(exit_date),
        "exit": exit_price,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": gross - costs,
    })

    return row


def close_scenario(entry, future, horizon):
    scenario = f"CLOSE_{horizon}D"

    if len(future) < horizon:
        latest = future[-1] if future else None

        return make_result(
            entry,
            scenario,
            latest["date"] if latest else None,
            latest["close"] if latest else None,
            "INSUFFICIENT_FUTURE_SESSIONS",
            False,
        )

    candle = future[horizon - 1]

    return make_result(
        entry,
        scenario,
        candle["date"],
        candle["close"],
        f"{horizon}D_CLOSE",
        True,
    )


def target_scenario(entry, future):
    scenario = "NO_STOP_TARGET_2_MAX_5D"
    selected = future[:MAX_FUTURE_SESSIONS]
    horizon_available = (
        len(future) >= MAX_FUTURE_SESSIONS
    )
    target = entry["entry"] * (
        1 + TARGET_PCT / 100
    )

    for candle in selected:
        if candle["open"] >= target:
            return make_result(
                entry,
                scenario,
                candle["date"],
                candle["open"],
                "TARGET_GAP",
                horizon_available,
            )

        if candle["high"] >= target:
            return make_result(
                entry,
                scenario,
                candle["date"],
                target,
                "TARGET_2_PERCENT",
                horizon_available,
            )

    if len(selected) < MAX_FUTURE_SESSIONS:
        latest = selected[-1] if selected else None

        return make_result(
            entry,
            scenario,
            latest["date"] if latest else None,
            latest["close"] if latest else None,
            "INSUFFICIENT_FUTURE_SESSIONS",
            False,
        )

    candle = selected[-1]

    return make_result(
        entry,
        scenario,
        candle["date"],
        candle["close"],
        "MAX_5D_CLOSE",
        True,
    )


def metrics(rows):
    completed = [
        row
        for row in rows
        if row.get("completed")
        and row.get("net_pnl") is not None
        and int(row.get("qty") or 0) > 0
    ]
    nets = [
        float(row["net_pnl"])
        for row in completed
    ]

    wins = sum(value > 0 for value in nets)
    losses = sum(value <= 0 for value in nets)

    profit = sum(
        value for value in nets if value > 0
    )
    loss = abs(sum(
        value for value in nets if value < 0
    ))

    return {
        "completed": len(completed),
        "unresolved": len(rows) - len(completed),
        "wins": wins,
        "losses": losses,
        "win_rate": (
            wins / len(completed) * 100
            if completed else 0
        ),
        "gross_pnl": sum(
            row["gross_pnl"] for row in completed
        ),
        "costs": sum(
            row["costs"] for row in completed
        ),
        "net_pnl": sum(nets),
        "expectancy": (
            sum(nets) / len(completed)
            if completed else 0
        ),
        "profit_factor": (
            profit / loss if loss else None
        ),
    }


entries = load_entries()
buys = [
    row for row in entries
    if row["direction"] == "BUY"
]
sells = [
    row for row in entries
    if row["direction"] == "SELL"
]

print("READ_ONLY_SWING_REPLAY=True")
print(f"UNIQUE_ENTRIES={len(entries)}")
print(f"BUY_DELIVERY_ELIGIBLE={len(buys)}")
print(f"SELL_OVERNIGHT_EXCLUDED={len(sells)}")
print("TARGET_PCT=2.0")
print("STOP_LOSS=None")
print("LIVE_STRATEGY_CHANGED=False")

kite = get_kite_client()
tokens = token_map(kite)

grouped = defaultdict(list)

for entry in buys:
    grouped[
        (entry["exchange"], entry["symbol"])
    ].append(entry)

all_results = []
failures = []

for index, ((exchange, symbol), rows) in enumerate(
    sorted(grouped.items()),
    start=1,
):
    token = tokens.get((exchange, symbol))

    if token is None:
        token = tokens.get(("NSE", symbol))
        exchange = "NSE"

    if token is None:
        failures.append(
            f"{exchange}:{symbol}:TOKEN_NOT_FOUND"
        )
        continue

    try:
        candles = fetch_daily(
            kite,
            token,
            exchange,
            symbol,
            min(
                row["timestamp"].date()
                for row in rows
            ) - timedelta(days=2),
            datetime.now(IST).date(),
        )
    except Exception as exc:
        failures.append(
            f"{exchange}:{symbol}:{exc}"
        )
        continue

    print(
        f"FETCH {index}/{len(grouped)} "
        f"{exchange}:{symbol} candles={len(candles)}"
    )

    for entry in rows:
        future = [
            candle
            for candle in candles
            if candle["date"]
            > entry["timestamp"].date()
        ]

        for horizon in (1, 3, 5):
            all_results.append(
                close_scenario(
                    entry,
                    future,
                    horizon,
                )
            )

        all_results.append(
            target_scenario(entry, future)
        )

scenarios = [
    "CLOSE_1D",
    "CLOSE_3D",
    "CLOSE_5D",
    "NO_STOP_TARGET_2_MAX_5D",
]

summary = {}

print("\nSWING RESULTS")
print("=" * 111)
print(
    f"{'SCENARIO':<29} {'DONE':>6} {'OPEN':>6} "
    f"{'W/L':>10} {'WIN%':>8} {'GROSS':>11} "
    f"{'COSTS':>10} {'NET':>11} {'EXP':>9} {'PF':>7}"
)

for scenario in scenarios:
    rows = [
        row for row in all_results
        if row["scenario"] == scenario
    ]

    value = metrics(rows)
    summary[scenario] = value

    pf = value["profit_factor"]
    pf_text = "INF" if pf is None else f"{pf:.2f}"

    print(
        f"{scenario:<29} "
        f"{value['completed']:>6} "
        f"{value['unresolved']:>6} "
        f"{value['wins']:>4}/{value['losses']:<4} "
        f"{value['win_rate']:>7.2f}% "
        f"{value['gross_pnl']:>+11.2f} "
        f"{value['costs']:>10.2f} "
        f"{value['net_pnl']:>+11.2f} "
        f"{value['expectancy']:>+9.2f} "
        f"{pf_text:>7}"
    )

report = {
    "method": {
        "capital_per_trade": CAPITAL_PER_TRADE,
        "direction": "BUY_DELIVERY_ONLY",
        "target_pct": TARGET_PCT,
        "stop_loss": None,
        "maximum_future_sessions": (
            MAX_FUTURE_SESSIONS
        ),
        "entry_day_target_check": False,
    },
    "source": {
        "unique_entries": len(entries),
        "buy_entries": len(buys),
        "sell_entries_excluded": len(sells),
        "fetch_failures": failures,
    },
    "summary": summary,
    "trades": all_results,
}

OUTPUT.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(f"\nDETAIL={OUTPUT}")
print(f"FETCH_FAILURES={len(failures)}")
