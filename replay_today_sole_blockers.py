#!/usr/bin/env python3
"""Replay current-logic sole blockers using audited entry data and 3m candles."""

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auth import get_kite_client
from costs import net_pnl_for_trade

IST = ZoneInfo("Asia/Kolkata")
DATE = "2026-08-19"
CURRENT_LOGIC_FROM = datetime.fromisoformat("2026-08-19T10:11:16+05:30")
AUDIT = Path("runtime/live_combined_audit/entry_audit.jsonl")
OUT = Path("runtime/sole_blocker_replay")

CAPITAL = 5000.0
RISK_PCT = 0.40
LEVERAGE = 4.0
STOP_PCT = 0.45
TARGET_PCT = 0.70
BODY_LIMIT = 1.50
EMA_LIMIT = 2.00
VWAP_LIMIT = 2.50

TARGET_REASONS = {
    "INDEPENDENT_ENTRY_CONFIRMATION_REQUIRED",
    "EXPECTED_MOVE_DOES_NOT_COVER_COSTS",
    "BREAKOUT_VALIDATION_FAILED",
    "VWAP_DIRECTION_NOT_ACCEPTED_OR_UNAVAILABLE",
    "INSUFFICIENT_ENTRY_CONFIRMATIONS",
    "ADX_STRENGTH_BELOW_MINIMUM_OR_UNAVAILABLE",
    "ADX_BELOW_MINIMUM_OR_UNAVAILABLE",
    "LOSS_REENTRY_COOLDOWN",
}


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=IST)
        return result.astimezone(IST)
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_candidates():
    rows, seen = [], set()
    counts = Counter()
    for line in AUDIT.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        logged = parse_time(row.get("logged_at"))
        if not logged or logged < CURRENT_LOGIC_FROM or logged.date().isoformat() != DATE:
            continue
        reasons = row.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if len(reasons) != 1 or reasons[0] not in TARGET_REASONS:
            continue
        reason = reasons[0]
        counts[reason] += 1
        candle = row.get("candle_eligibility") or {}
        detail = candle.get("detail") or {}
        observations = row.get("observations") or {}
        entry_last = observations.get("entry_last") or {}
        cost = detail.get("cost_aware_movement") or {}
        breakout = detail.get("breakout_validation") or row.get("breakout_validation") or {}
        metrics = breakout.get("metrics") or {}
        signal_time = parse_time(row.get("candle_time"))
        direction = detail.get("direction") or row.get("final_direction") or row.get("ema_base_direction")
        entry = number(detail.get("entry_close"))
        atr = number(cost.get("recent_true_range")) or number(metrics.get("atr_14"))
        ema = number(entry_last.get("ema_entry"))
        open_price = number(entry_last.get("open"))
        vwap = number(detail.get("vwap"))
        quantity = int(number(cost.get("quantity")) or 0)
        key = (reason, row.get("symbol"), direction, signal_time)
        if key in seen:
            continue
        seen.add(key)
        complete = all(x is not None for x in (signal_time, direction, entry, atr, ema, open_price, vwap))
        if complete and direction not in ("BUY", "SELL"):
            complete = False
        body_atr = abs(entry - open_price) / atr if complete and atr > 0 else None
        ema_atr = abs(entry - ema) / atr if complete and atr > 0 else None
        vwap_atr = abs(entry - vwap) / atr if complete and atr > 0 else None
        quality_pass = bool(
            complete and body_atr <= BODY_LIMIT and ema_atr <= EMA_LIMIT and vwap_atr <= VWAP_LIMIT
        )
        rows.append({
            "reason": reason,
            "symbol": row.get("symbol") or "UNKNOWN",
            "time": signal_time,
            "direction": direction,
            "entry": entry,
            "atr": atr,
            "quantity": quantity,
            "body_atr": body_atr,
            "ema_atr": ema_atr,
            "vwap_atr": vwap_atr,
            "quality_pass": quality_pass,
            "complete": complete,
            "adx": number(row.get("adx")),
            "breakout_failures": breakout.get("reasons") or [],
        })
    return rows, counts


def tokens(kite):
    return {row["tradingsymbol"]: row["instrument_token"] for row in kite.instruments("NSE")}


def fetch(kite, token):
    day = datetime.fromisoformat(DATE).date()
    start = datetime.combine(day, dt_time(9, 15), tzinfo=IST)
    end = datetime.combine(day, dt_time(15, 30), tzinfo=IST)
    for attempt in range(8):
        try:
            time.sleep(0.5)
            result = kite.historical_data(token, start, end, "3minute")
            for row in result:
                row["date"] = parse_time(row.get("date"))
            return result
        except Exception as exc:
            wait = min(2 ** attempt, 30)
            print(f"API retry {attempt + 1}: {exc}; waiting {wait}s")
            time.sleep(wait)
    return []


def replay(candidate, candles):
    entry = candidate["entry"]
    if candidate["direction"] == "BUY":
        stop = entry * (1 - STOP_PCT / 100)
        target = entry * (1 + TARGET_PCT / 100)
    else:
        stop = entry * (1 + STOP_PCT / 100)
        target = entry * (1 - TARGET_PCT / 100)
    usable = [
        row for row in candles
        if row.get("date") and row["date"] >= candidate["time"] + timedelta(minutes=3)
        and row["date"].time() <= dt_time(15, 15)
    ]
    for row in usable:
        if candidate["direction"] == "BUY":
            stop_hit, target_hit = row["low"] <= stop, row["high"] >= target
        else:
            stop_hit, target_hit = row["high"] >= stop, row["low"] <= target
        if stop_hit:
            return stop, row["date"], "STOP"
        if target_hit:
            return target, row["date"], "TARGET"
    if usable:
        return usable[-1]["close"], usable[-1]["date"], "SQUARE_OFF"
    return None


def calculate_pnl(candidate, exit_price):
    risk_per_share = candidate["entry"] * STOP_PCT / 100
    qty_risk = int((CAPITAL * RISK_PCT / 100) / risk_per_share)
    qty_margin = int((CAPITAL * LEVERAGE) / candidate["entry"])
    qty = min(qty_risk, qty_margin)
    if candidate["quantity"] > 0:
        qty = min(qty, candidate["quantity"])
    if qty <= 0:
        return None
    result = net_pnl_for_trade(candidate["direction"], qty, candidate["entry"], exit_price)
    gross = float(result["gross_pnl"])
    net = float(result["net_pnl"])
    return qty, gross, gross - net, net


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    candidates, raw_counts = load_candidates()
    print("CURRENT-LOGIC SOLE-BLOCKER REPLAY")
    print("Date:", DATE)
    print("Raw sole-blocker evaluations:", dict(raw_counts))
    print("Deduplicated candidates:", len(candidates))
    complete = [row for row in candidates if row["complete"]]
    quality = [row for row in complete if row["quality_pass"]]
    print("Complete audited entries:", len(complete))
    print("Would pass current entry quality:", len(quality))
    print("Unreplayable after early short-circuit:", len(candidates) - len(complete))

    grouped_all = Counter(row["reason"] for row in candidates)
    grouped_complete = Counter(row["reason"] for row in complete)
    grouped_quality = Counter(row["reason"] for row in quality)
    print("\nFUNNEL BY SOLE BLOCKER")
    for reason in sorted(grouped_all):
        print(f"{reason:<55} unique={grouped_all[reason]:>3} complete={grouped_complete[reason]:>3} quality_pass={grouped_quality[reason]:>3}")

    if not quality:
        return
    kite = get_kite_client()
    token_map = tokens(kite)
    cache = {}
    trades = []
    for index, candidate in enumerate(quality, 1):
        symbol = candidate["symbol"]
        if symbol not in cache:
            print(f"Fetching {index}/{len(quality)}: NSE:{symbol}")
            token = token_map.get(symbol)
            cache[symbol] = fetch(kite, token) if token else []
        exit_result = replay(candidate, cache[symbol])
        if not exit_result:
            continue
        exit_price, exit_time, exit_reason = exit_result
        pnl = calculate_pnl(candidate, exit_price)
        if not pnl:
            continue
        qty, gross, costs, net = pnl
        trade = dict(candidate)
        trade.update(exit=exit_price, exit_time=exit_time, exit_reason=exit_reason,
                     qty=qty, gross=gross, costs=costs, net=net)
        trades.append(trade)

    print("\nINDIVIDUAL COUNTERFACTUAL TRADES")
    for row in sorted(trades, key=lambda item: item["time"]):
        print(
            f"{row['time'].strftime('%H:%M')} {row['reason']:<48} {row['symbol']:<13} "
            f"{row['direction']:<4} body={row['body_atr']:.3f} ema={row['ema_atr']:.3f} "
            f"vwap={row['vwap_atr']:.3f} {row['exit_reason']:<10} net=₹{row['net']:.2f}"
        )

    print("\nRESULT BY RELAXED FILTER")
    by_reason = defaultdict(list)
    for row in trades:
        by_reason[row["reason"]].append(row)
    for reason, rows in sorted(by_reason.items()):
        wins = sum(row["net"] > 0 for row in rows)
        net = sum(row["net"] for row in rows)
        costs = sum(row["costs"] for row in rows)
        print(f"{reason:<55} trades={len(rows):>3} wins={wins:>3} losses={len(rows)-wins:>3} costs=₹{costs:.2f} net=₹{net:.2f}")

    print("\nCAUTION")
    print("Results use the audited entry close and current fixed 0.45% stop / 0.70% target.")
    print("Stop is assumed first when stop and target occur inside the same 3-minute candle.")
    print("ADX-hard and cooldown rows that lack a direction/entry are not guessed.")


if __name__ == "__main__":
    main()
