#!/usr/bin/env python3

import csv
import json
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from auth import get_kite_client
from costs import net_pnl_for_trade

IST = ZoneInfo("Asia/Kolkata")

DATE = "2026-08-19"
NEW_LOGIC_FROM = datetime.fromisoformat(
    "2026-08-19T10:11:16+05:30"
)

VALIDATION = Path(
    f"validation_events/{DATE}.jsonl"
)

OUTPUT_DIR = Path(
    "runtime/today_parameter_grid"
)

CAPITAL = float(config.CAPITAL)
LEVERAGE = 4.0
VWAP_LIMIT = 2.50
MAX_TRADES = 7
MAX_OPEN = 1
DAILY_LOSS_PCT = 0.50

BODY_LIMITS = (
    1.50,
    1.55,
    1.60,
    1.70,
    1.80,
    2.00,
)

EMA_LIMITS = (
    2.00,
    2.25,
    2.50,
    2.75,
    3.00,
)

RISK_LEVELS = (
    0.20,
    0.30,
    0.40,
    0.50,
    0.75,
    1.00,
)


def number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=IST)

        return result.astimezone(IST)
    except (TypeError, ValueError):
        return None


def read_candidates():
    candidates = []
    seen = set()

    if not VALIDATION.exists():
        raise SystemExit(
            f"Missing validation file: {VALIDATION}"
        )

    for line in VALIDATION.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        if not line.strip():
            continue

        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = row.get("event_type")
        if event_type not in (
            "candidate_collected",
            "candidate_rejected",
        ):
            continue

        recorded_at = parse_datetime(
            row.get("recorded_at")
        )

        # Use only candidates evaluated after the new
        # independent-confirmation logic became active.
        if (
            recorded_at is None
            or recorded_at < NEW_LOGIC_FROM
        ):
            continue

        payload = row.get("payload") or {}
        signal = payload.get("signal") or payload

        quality = (
            payload.get("entry_quality_detail")
            or signal.get("entry_quality_detail")
            or {}
        )

        symbol = (
            payload.get("symbol")
            or signal.get("symbol")
        )

        exchange = (
            payload.get("exchange")
            or signal.get("exchange")
            or "NSE"
        )

        direction = signal.get("direction")

        timestamp = parse_datetime(
            signal.get("timestamp")
            or payload.get("timestamp")
            or recorded_at
        )

        entry = number(
            signal.get("entry_price")
        )

        stop = number(
            signal.get("stop_loss")
            or payload.get("stop_loss")
        )

        target = number(
            signal.get("target")
            or payload.get("target")
        )

        atr = number(quality.get("atr"))
        ema = number(
            quality.get("ema_distance_atr")
        )
        body = number(
            quality.get("signal_body_atr")
        )
        vwap = number(
            quality.get("vwap_distance_atr")
        )

        if not all((
            symbol,
            direction in ("BUY", "SELL"),
            timestamp,
            entry is not None,
            stop is not None,
            target is not None,
            ema is not None,
            body is not None,
            vwap is not None,
        )):
            continue

        key = (
            symbol,
            direction,
            timestamp.isoformat(),
        )

        if key in seen:
            continue

        seen.add(key)

        candidates.append({
            "date": DATE,
            "timestamp": timestamp,
            "recorded_at": recorded_at,
            "symbol": symbol,
            "exchange": exchange,
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "target": target,
            "atr": atr,
            "ema": ema,
            "body": body,
            "vwap": vwap,
            "original_event": event_type,
            "original_reason": (
                payload.get("reason_code") or ""
            ),
        })

    return sorted(
        candidates,
        key=lambda row: row["timestamp"],
    )


def instrument_tokens(kite, exchanges):
    mapping = {}

    for exchange in sorted(exchanges):
        print(
            f"Loading {exchange} instrument list..."
        )

        for instrument in kite.instruments(exchange):
            symbol = instrument.get(
                "tradingsymbol"
            )
            token = instrument.get(
                "instrument_token"
            )

            if symbol and token:
                mapping[(exchange, symbol)] = token

    return mapping


def download_candles(kite, token):
    start = datetime.combine(
        datetime.fromisoformat(DATE).date(),
        clock_time(9, 15),
        tzinfo=IST,
    )

    end = datetime.combine(
        datetime.fromisoformat(DATE).date(),
        clock_time(15, 30),
        tzinfo=IST,
    )

    for attempt in range(8):
        try:
            time.sleep(0.50)

            rows = kite.historical_data(
                token,
                start,
                end,
                "3minute",
            )

            cleaned = []

            for row in rows:
                stamp = parse_datetime(
                    row.get("date")
                )

                if stamp is None:
                    continue

                cleaned.append({
                    "time": stamp,
                    "open": number(row.get("open")),
                    "high": number(row.get("high")),
                    "low": number(row.get("low")),
                    "close": number(row.get("close")),
                })

            return cleaned

        except Exception as exc:
            wait = min(2 ** attempt, 30)

            print(
                f"Historical API retry "
                f"{attempt + 1}/8 after {wait}s: "
                f"{exc}"
            )

            time.sleep(wait)

    return []


def raw_exit(candidate, candles):
    # Start on the candle after the signal candle.
    first_exit_candle = (
        candidate["timestamp"]
        + timedelta(minutes=3)
    )

    usable = [
        candle
        for candle in candles
        if candle["time"] >= first_exit_candle
        and candle["time"].time()
        <= clock_time(15, 15)
    ]

    for candle in usable:
        high = candle["high"]
        low = candle["low"]

        if high is None or low is None:
            continue

        if candidate["direction"] == "BUY":
            stop_hit = low <= candidate["stop"]
            target_hit = high >= candidate["target"]
        else:
            stop_hit = high >= candidate["stop"]
            target_hit = low <= candidate["target"]

        # Conservative assumption if both occur inside
        # the same three-minute candle: stop happens first.
        if stop_hit:
            return {
                "exit": candidate["stop"],
                "exit_time": candle["time"],
                "exit_reason": "STOP",
            }

        if target_hit:
            return {
                "exit": candidate["target"],
                "exit_time": candle["time"],
                "exit_reason": "TARGET",
            }

    if usable:
        return {
            "exit": usable[-1]["close"],
            "exit_time": usable[-1]["time"],
            "exit_reason": "SQUARE_OFF",
        }

    return None


def calculate_quantity(candidate, risk_pct):
    risk_per_share = abs(
        candidate["entry"] - candidate["stop"]
    )

    if risk_per_share <= 0:
        return 0

    planned_risk = (
        CAPITAL * risk_pct / 100
    )

    quantity_by_risk = int(
        planned_risk / risk_per_share
    )

    quantity_by_margin = int(
        (CAPITAL * LEVERAGE)
        / candidate["entry"]
    )

    return max(
        0,
        min(
            quantity_by_risk,
            quantity_by_margin,
        ),
    )


def simulate(
    candidates,
    exits,
    body_limit,
    ema_limit,
    risk_pct,
):
    eligible = [
        candidate
        for candidate in candidates
        if candidate["body"] <= body_limit
        and candidate["ema"] <= ema_limit
        and candidate["vwap"] <= VWAP_LIMIT
        and exits.get(
            (
                candidate["exchange"],
                candidate["symbol"],
                candidate["timestamp"],
            )
        )
    ]

    trades = []
    next_free = None
    day_net = 0.0
    daily_loss_limit = (
        CAPITAL * DAILY_LOSS_PCT / 100
    )

    for candidate in eligible:
        if len(trades) >= MAX_TRADES:
            break

        if (
            next_free is not None
            and candidate["timestamp"] < next_free
        ):
            continue

        if day_net <= -daily_loss_limit:
            break

        quantity = calculate_quantity(
            candidate,
            risk_pct,
        )

        if quantity <= 0:
            continue

        result = exits[
            (
                candidate["exchange"],
                candidate["symbol"],
                candidate["timestamp"],
            )
        ]

        pnl = net_pnl_for_trade(
            candidate["direction"],
            quantity,
            candidate["entry"],
            result["exit"],
        )

        gross = float(pnl["gross_pnl"])
        costs = float(
            pnl.get("costs")
            or pnl.get("charges")
            or gross - float(pnl["net_pnl"])
        )
        net = float(pnl["net_pnl"])

        trade = {
            **candidate,
            **result,
            "quantity": quantity,
            "gross_pnl": gross,
            "costs": costs,
            "net_pnl": net,
        }

        trades.append(trade)
        day_net += net

        if MAX_OPEN == 1:
            next_free = result["exit_time"]

    wins = sum(
        trade["net_pnl"] > 0
        for trade in trades
    )

    losses = sum(
        trade["net_pnl"] <= 0
        for trade in trades
    )

    return {
        "eligible": len(eligible),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "gross": sum(
            trade["gross_pnl"]
            for trade in trades
        ),
        "costs": sum(
            trade["costs"]
            for trade in trades
        ),
        "net": sum(
            trade["net_pnl"]
            for trade in trades
        ),
    }


def main():
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    candidates = read_candidates()

    print(
        "Post-update candidates with complete data:",
        len(candidates),
    )

    if not candidates:
        raise SystemExit(
            "No complete candidates found."
        )

    kite = get_kite_client()

    tokens = instrument_tokens(
        kite,
        {
            candidate["exchange"]
            for candidate in candidates
        },
    )

    candle_cache = {}
    exits = {}

    unique_symbols = sorted({
        (
            candidate["exchange"],
            candidate["symbol"],
        )
        for candidate in candidates
    })

    for index, (exchange, symbol) in enumerate(
        unique_symbols,
        start=1,
    ):
        print(
            f"Fetching {index}/{len(unique_symbols)}: "
            f"{exchange}:{symbol}"
        )

        token = tokens.get((exchange, symbol))

        candle_cache[(exchange, symbol)] = (
            download_candles(kite, token)
            if token else []
        )

    for candidate in candidates:
        candles = candle_cache.get(
            (
                candidate["exchange"],
                candidate["symbol"],
            ),
            [],
        )

        result = raw_exit(
            candidate,
            candles,
        )

        if result:
            exits[
                (
                    candidate["exchange"],
                    candidate["symbol"],
                    candidate["timestamp"],
                )
            ] = result

    summary_rows = []
    trade_rows = []

    print()
    print(
        "Body  EMA   Risk  Eligible Trades "
        "Wins Losses Gross Costs Net"
    )

    for body_limit in BODY_LIMITS:
        for ema_limit in EMA_LIMITS:
            for risk_pct in RISK_LEVELS:
                result = simulate(
                    candidates,
                    exits,
                    body_limit,
                    ema_limit,
                    risk_pct,
                )

                row = {
                    "body_limit_atr": body_limit,
                    "ema_limit_atr": ema_limit,
                    "vwap_limit_atr": VWAP_LIMIT,
                    "risk_pct": risk_pct,
                    "eligible": result["eligible"],
                    "trades": len(result["trades"]),
                    "wins": result["wins"],
                    "losses": result["losses"],
                    "win_rate_pct": (
                        result["wins"]
                        / len(result["trades"])
                        * 100
                        if result["trades"] else 0
                    ),
                    "gross_pnl": result["gross"],
                    "costs": result["costs"],
                    "net_pnl": result["net"],
                }

                summary_rows.append(row)

                print(
                    f"{body_limit:>4.2f} "
                    f"{ema_limit:>4.2f} "
                    f"{risk_pct:>4.2f}% "
                    f"{result['eligible']:>8} "
                    f"{len(result['trades']):>6} "
                    f"{result['wins']:>4} "
                    f"{result['losses']:>6} "
                    f"₹{result['gross']:>7.2f} "
                    f"₹{result['costs']:>6.2f} "
                    f"₹{result['net']:>7.2f}"
                )

                for trade in result["trades"]:
                    trade_rows.append({
                        "body_limit_atr": body_limit,
                        "ema_limit_atr": ema_limit,
                        "risk_pct": risk_pct,
                        "symbol": trade["symbol"],
                        "direction": trade["direction"],
                        "signal_time": (
                            trade["timestamp"].isoformat()
                        ),
                        "entry": trade["entry"],
                        "stop": trade["stop"],
                        "target": trade["target"],
                        "exit": trade["exit"],
                        "exit_time": (
                            trade["exit_time"].isoformat()
                        ),
                        "exit_reason": (
                            trade["exit_reason"]
                        ),
                        "quantity": trade["quantity"],
                        "body_atr": trade["body"],
                        "ema_distance_atr": trade["ema"],
                        "vwap_distance_atr": trade["vwap"],
                        "gross_pnl": trade["gross_pnl"],
                        "costs": trade["costs"],
                        "net_pnl": trade["net_pnl"],
                    })

    summary_path = OUTPUT_DIR / "summary.csv"
    trades_path = OUTPUT_DIR / "trades.csv"

    with summary_path.open(
        "w",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=summary_rows[0].keys(),
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    if trade_rows:
        with trades_path.open(
            "w",
            newline="",
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=trade_rows[0].keys(),
            )
            writer.writeheader()
            writer.writerows(trade_rows)

    ranked = sorted(
        summary_rows,
        key=lambda row: (
            row["net_pnl"],
            row["win_rate_pct"],
            -row["risk_pct"],
        ),
        reverse=True,
    )

    print()
    print("TOP 10 BY NET P&L")
    print("-----------------")

    for row in ranked[:10]:
        print(
            f"Body={row['body_limit_atr']:.2f} "
            f"EMA={row['ema_limit_atr']:.2f} "
            f"Risk={row['risk_pct']:.2f}% | "
            f"trades={row['trades']} "
            f"wins={row['wins']} "
            f"losses={row['losses']} "
            f"win-rate={row['win_rate_pct']:.2f}% "
            f"net=₹{row['net_pnl']:.2f}"
        )

    print()
    print("Reports:", OUTPUT_DIR.resolve())
    print()
    print(
        "NOTE: This uses stop/target/15:15 square-off. "
        "It is a conservative counterfactual replay, "
        "not a guarantee of live fills."
    )


if __name__ == "__main__":
    main()
