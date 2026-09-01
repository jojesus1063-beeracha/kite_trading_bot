#!/usr/bin/env python3
"""Read-only enriched replay of the clean PAPER entry policy.

The replay cohort is every unique historical entry retained in
trade_history.jsonl. Required OHLCV is re-fetched from Kite so TA-Lib,
EMA200, VWAP and ADX are evaluated from point-in-time candles rather than
missing audit fields. It never places an order or writes trading state.

This is a historical-entry-cohort replay, not a full-universe discovery
backtest: symbols/timestamps never selected by an older bot are unknowable
unless their daily watchlist and scan data were retained.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

import config as cfg
from auth import get_kite_client
from candle_eligibility import evaluate_candle_eligibility
from costs import net_pnl_for_trade
from entry_confirmation import assess_entry_context
from entry_quality import assess_entry_quality
from entry_timing import INVALID as TIMING_INVALID, evaluate_entry_timing
from indicators import add_indicators, ema
from price_action import evaluate_price_action
from rvol import passes_rvol_threshold
from strategy import Signal
from watchlist_filters import classify_direction_eligibility


IST = "Asia/Kolkata"
ENTRY_MINUTES = 3
TREND_MINUTES = 15
ENTRY_START = "09:25"
ENTRY_END = "15:00"
SQUARE_OFF = "15:08"


@dataclass
class Candidate:
    key: str
    date: str
    symbol: str
    exchange: str
    timestamp: pd.Timestamp
    old_direction: str
    old_entry: float


def configure_replay() -> None:
    cfg.PAPER_TRADING = True
    cfg.ENTRY_TIMEFRAME = "3minute"
    cfg.PAPER_ADX_MIN_STRENGTH = 20.0
    cfg.PAPER_BUY_MIN_ADX = 25.0
    cfg.PAPER_SELL_MIN_ADX = 20.0
    cfg.PAPER_CANDLE_VOLUME_LOOKBACK = 20
    cfg.PAPER_CANDLE_MIN_VOLUME_RATIO = 1.2
    cfg.PAPER_CANDLE_REQUIRED_CONFIRMATIONS = 2
    cfg.PAPER_REQUIRE_EMA200_ALIGNMENT = False
    cfg.PAPER_ENABLE_COST_AWARE_GATE = True
    cfg.PAPER_COST_MOVE_LOOKBACK = 14
    cfg.PAPER_EXPECTED_MOVE_ATR_MULTIPLIER = 1.0
    cfg.PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE = 2.0
    cfg.PAPER_CANDLE_MAX_FRESH_SECONDS = 90.0
    cfg.PAPER_CANDLE_COMPLETION_GRACE_SECONDS = 5.0
    cfg.ENABLE_RVOL_FILTER = False
    cfg.RVOL_THRESHOLD = 1.2
    cfg.ENABLE_200_EMA_FILTER = False
    cfg.ENABLE_EMA200_WATCHLIST = False
    cfg.ENABLE_ENTRY_TIMING_FILTER = True
    cfg.ENABLE_CONFIRMATION_QUALITY_FILTER = False
    cfg.ENABLE_VOLUME_ACCELERATION_FILTER = False
    cfg.ENABLE_PRICE_ACTION = True
    cfg.PAPER_PRICE_ACTION_OBSERVATIONAL = True


def ist_timestamp(value, date_hint=None) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if date_hint and len(text) <= 8:
            text = f"{date_hint} {text}"
        ts = pd.Timestamp(text)
        if ts.tzinfo is None:
            return ts.tz_localize(IST)
        return ts.tz_convert(IST)
    except Exception:
        return None


def load_candidates(path: Path) -> list[Candidate]:
    groups: dict[str, dict] = {}
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
                    entry_time=row.get("entry_time") or row.get("time") or line_number,
                )
            groups.setdefault(key, row)

    candidates = []
    for key, row in groups.items():
        date = str(row.get("date"))
        timestamp = (
            ist_timestamp(row.get("signal_candle_start"), date)
            or ist_timestamp(row.get("entry_time"), date)
            or ist_timestamp(row.get("time"), date)
        )
        if timestamp is None:
            continue
        # Kite timestamps a 3-minute bar at its start. Normalize delayed fill
        # timestamps back to the containing exchange-aligned candle.
        session = timestamp.normalize() + pd.Timedelta(hours=9, minutes=15)
        elapsed = max(0, int((timestamp - session).total_seconds() // 60))
        timestamp = session + pd.Timedelta(minutes=(elapsed // ENTRY_MINUTES) * ENTRY_MINUTES)
        candidates.append(Candidate(
            key=key,
            date=date,
            symbol=str(row.get("symbol")),
            exchange=str(row.get("exchange") or "NSE"),
            timestamp=timestamp,
            old_direction=str(row.get("direction") or "").upper(),
            old_entry=float(row.get("entry") or 0.0),
        ))
    return sorted(candidates, key=lambda item: (item.timestamp, item.symbol))


def fetch_frame(kite, token, interval, start, end, attempts=3) -> pd.DataFrame:
    error = None
    for attempt in range(attempts):
        try:
            data = kite.historical_data(token, start, end, interval)
            frame = pd.DataFrame(data)
            if frame.empty:
                return frame
            frame["date"] = pd.to_datetime(frame["date"])
            if frame["date"].dt.tz is None:
                frame["date"] = frame["date"].dt.tz_localize(IST)
            else:
                frame["date"] = frame["date"].dt.tz_convert(IST)
            return frame[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)
        except Exception as exc:
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"historical_data failed after {attempts} attempts: {error}")


def instrument_map(kite) -> dict[tuple[str, str], int]:
    result = {}
    for exchange in ("NSE", "BSE"):
        for item in kite.instruments(exchange):
            symbol = str(item.get("tradingsymbol") or "")
            if symbol:
                result[(exchange, symbol)] = int(item["instrument_token"])
    return result


def normal_ema_direction(frame: pd.DataFrame) -> tuple[str | None, float | None, float | None]:
    if frame is None or len(frame) < 21:
        return None, None, None
    close = pd.to_numeric(frame["close"], errors="coerce")
    e9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
    e21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    if pd.isna(e9) or pd.isna(e21) or e9 == e21:
        return None, None, None
    return ("BUY" if e9 > e21 else "SELL"), float(e9), float(e21)


def point_in_time_frames(data, candidate):
    entry_all, trend_all = data
    candle_start = candidate.timestamp
    decision_time = candle_start + pd.Timedelta(minutes=ENTRY_MINUTES)
    entry = entry_all.loc[entry_all["date"] <= candle_start].copy()
    trend = trend_all.loc[
        trend_all["date"] + pd.Timedelta(minutes=TREND_MINUTES) <= decision_time
    ].copy()
    return entry, trend, decision_time


def technical_decision(data, candidate):
    entry, trend, decision_time = point_in_time_frames(data, candidate)
    if entry.empty or trend.empty or entry.iloc[-1]["date"] != candidate.timestamp:
        return False, "MISSING_POINT_IN_TIME_CANDLE", {}, None

    direction, e9, e21 = normal_ema_direction(entry)
    if direction is None:
        return False, "EMA_DIRECTION_UNAVAILABLE", {}, None

    pa_score, pa_detail = evaluate_price_action(entry, direction, cfg)
    gate = evaluate_candle_eligibility(
        entry,
        trend.tail(1),
        direction,
        cfg,
        now=decision_time + pd.Timedelta(seconds=12),
        price_action_score=pa_score,
    )
    detail = {
        "candle_gate": gate.to_dict(),
        "ema9": e9,
        "ema21": e21,
        "price_action": {"score": pa_score, **(pa_detail or {})},
    }
    if not gate.accepted:
        return False, "CANDLE:" + ",".join(gate.reasons), detail, direction

    eligibility, eligibility_detail = classify_direction_eligibility(trend, cfg)
    detail["ema200_watchlist"] = {"eligibility": eligibility, **eligibility_detail}
    if (
        bool(getattr(cfg, "PAPER_REQUIRE_EMA200_ALIGNMENT", True))
        and eligibility != direction
    ):
        return False, "EMA200_WATCHLIST_DIRECTION", detail, direction

    rvol_ok, rvol_value, rvol_detail = passes_rvol_threshold(entry, cfg)
    detail["rvol"] = {"passed": rvol_ok, "value": rvol_value, **rvol_detail}
    if not rvol_ok:
        return False, "RVOL", detail, direction

    current = entry.iloc[-1]
    previous = entry.iloc[-2] if len(entry) >= 2 else None
    timing, timing_detail = evaluate_entry_timing(
        candidate.symbol, direction, entry, current, previous, cfg
    )
    detail["entry_timing"] = {"classification": timing, **timing_detail}
    if timing == TIMING_INVALID:
        return False, "ENTRY_TIMING", detail, direction

    entry_price = float(current["close"])
    geometric_stop = entry_price * (0.9955 if direction == "BUY" else 1.0045)
    signal = Signal(
        symbol=candidate.symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=geometric_stop,
        target=entry_price * (1.007 if direction == "BUY" else 0.993),
        timestamp=current["date"],
        reason="historical clean candle replay",
        price_action_score=float(pa_score),
        price_action_detail=pa_detail,
    )
    quality = assess_entry_quality(signal, entry)
    detail["entry_quality"] = {
        "accepted": quality.accepted,
        "score": quality.score,
        "reason": quality.reason,
        "detail": quality.detail,
    }
    if not quality.accepted:
        return False, "ENTRY_QUALITY", detail, direction

    context = assess_entry_context(signal, trend)
    detail["entry_context"] = {
        "accepted": context.accepted,
        "score_adjustment": context.score_adjustment,
        "reason": context.reason,
        "detail": context.detail,
    }
    if not context.accepted:
        return False, "ENTRY_CONTEXT", detail, direction

    return True, "PASS", detail, direction


def excursion(direction, entry, bars):
    high = float(bars["high"].max())
    low = float(bars["low"].min())
    last = float(bars.iloc[-1]["close"])
    if direction == "BUY":
        mfe = (high - entry) / entry * 100
        mae = (low - entry) / entry * 100
        current = (last - entry) / entry * 100
    else:
        mfe = (entry - low) / entry * 100
        mae = (entry - high) / entry * 100
        current = (entry - last) / entry * 100
    mfe, mae = max(0.0, mfe), min(0.0, mae)
    giveback = 0.0 if mfe <= 0 else max(0.0, (mfe - current) / mfe * 100)
    return mfe, mae, current, giveback


def adverse_three(direction, completed):
    if len(completed) < 3:
        return False
    recent = completed.tail(3)
    if direction == "BUY":
        flags = (recent["close"] < recent["replay_ema9"]) & (recent["replay_ema9"] < recent["replay_ema21"])
    else:
        flags = (recent["close"] > recent["replay_ema9"]) & (recent["replay_ema9"] > recent["replay_ema21"])
    return bool(flags.all())


def mfe_time_reason(minutes, mfe, current, giveback):
    if minutes < 20:
        return None
    if minutes >= 40 and mfe < 0.30 and current < 0:
        return "mfe_time_dead_loser_40m"
    if minutes > 40:
        return "mfe_time_late_giveback" if mfe >= 0.30 and giveback >= 50 else None
    if mfe >= 0.50 and current <= 0.30:
        return "mfe_time_lock_20_40"
    if mfe >= 0.40 and giveback >= 50:
        return "mfe_time_giveback_20_40"
    return None


def leg_result(direction, qty, entry, exit_price):
    if qty <= 0:
        return {"gross_pnl": 0.0, "costs": 0.0, "net_pnl": 0.0}
    return net_pnl_for_trade(direction, qty, entry, exit_price)


def simulate_exit(entry_all, candidate, direction):
    work = entry_all.copy()
    close = pd.to_numeric(work["close"], errors="coerce")
    work["replay_ema9"] = close.ewm(span=9, adjust=False).mean()
    work["replay_ema21"] = close.ewm(span=21, adjust=False).mean()
    after = work.loc[work["date"] > candidate.timestamp].copy()
    after = after.loc[after["date"].dt.date == candidate.timestamp.date()]
    if after.empty:
        return None

    entry_time = after.iloc[0]["date"]
    entry = float(after.iloc[0]["open"])
    risk_rupees = float(getattr(cfg, "CAPITAL", 5000.0)) * 0.20 / 100.0
    sizing_risk = entry * 0.0045
    qty = int(risk_rupees / sizing_risk) if sizing_risk > 0 else 0
    if qty <= 0:
        return None

    hybrid = qty >= 2
    scalp_qty = max(1, min(qty - 1, math.floor(qty * 0.5))) if hybrid else 0
    remaining = qty
    stage = "SCALP" if hybrid else "FIXED"
    sign = 1.0 if direction == "BUY" else -1.0
    scalp_target = entry + sign * sizing_risk
    runner_target = entry + sign * sizing_risk * 2.0
    fixed_target = entry * (1.007 if direction == "BUY" else 0.993)
    stop = entry * (0.9925 if direction == "BUY" else 1.0075)
    legs = []
    seen = after.iloc[0:0].copy()

    def stop_hit(row):
        return float(row["low"]) <= stop if direction == "BUY" else float(row["high"]) >= stop

    def target_hit(row, target):
        return float(row["high"]) >= target if direction == "BUY" else float(row["low"]) <= target

    for _, row in after.iterrows():
        seen = pd.concat([seen, row.to_frame().T], ignore_index=True)
        bar_close_time = row["date"] + pd.Timedelta(minutes=ENTRY_MINUTES)
        minutes = max(0.0, (bar_close_time - entry_time).total_seconds() / 60.0)

        # Conservative intrabar convention: if a stop and target coexist in
        # one candle, assume the stop was reached first.
        if stop_hit(row):
            result = leg_result(direction, remaining, entry, stop)
            legs.append({"qty": remaining, "exit": stop, "reason": "breakeven_stop" if stop == entry else "emergency_stop", **result})
            remaining = 0
        elif stage == "SCALP" and target_hit(row, scalp_target):
            result = leg_result(direction, scalp_qty, entry, scalp_target)
            legs.append({"qty": scalp_qty, "exit": scalp_target, "reason": "hybrid_scalp_1r", **result})
            remaining -= scalp_qty
            stage = "RUNNER"
            stop = entry
        elif stage == "RUNNER" and target_hit(row, runner_target):
            result = leg_result(direction, remaining, entry, runner_target)
            legs.append({"qty": remaining, "exit": runner_target, "reason": "hybrid_runner_2r", **result})
            remaining = 0
        elif stage == "FIXED" and target_hit(row, fixed_target):
            result = leg_result(direction, remaining, entry, fixed_target)
            legs.append({"qty": remaining, "exit": fixed_target, "reason": "fixed_target", **result})
            remaining = 0

        if remaining <= 0:
            return entry_time, row["date"], entry, qty, legs

        mfe, mae, current, giveback = excursion(direction, entry, seen)
        reason = None
        if minutes > 10 and mae <= -0.30 and current <= -0.15 and mfe < 0.30 and adverse_three(direction, seen):
            reason = "mae_adverse_trend_10m"
        if reason is None:
            reason = mfe_time_reason(minutes, mfe, current, giveback)
        if reason:
            exit_price = float(row["close"])
            result = leg_result(direction, remaining, entry, exit_price)
            legs.append({"qty": remaining, "exit": exit_price, "reason": reason, **result})
            return entry_time, row["date"], entry, qty, legs

        if bar_close_time.time() >= pd.Timestamp(SQUARE_OFF).time():
            exit_price = float(row["close"])
            result = leg_result(direction, remaining, entry, exit_price)
            legs.append({"qty": remaining, "exit": exit_price, "reason": "eod_square_off", **result})
            return entry_time, row["date"], entry, qty, legs
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default="trade_history.jsonl")
    parser.add_argument("--output", default="runtime/replay_clean_candle_all_days.json")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    args = parser.parse_args()

    configure_replay()
    candidates = load_candidates(Path(args.history))
    if args.from_date:
        candidates = [c for c in candidates if c.date >= args.from_date]
    if args.to_date:
        candidates = [c for c in candidates if c.date <= args.to_date]
    if not candidates:
        raise SystemExit("No unique historical entries found")

    print("READ_ONLY_REPLAY=True")
    print("COHORT=UNIQUE_RETAINED_HISTORICAL_ENTRIES")
    print(f"CANDIDATES={len(candidates)} DATES={candidates[0].date}..{candidates[-1].date}")
    print("IMPORTANT=Not a full-universe discovery backtest")

    kite = get_kite_client()
    tokens = instrument_map(kite)
    by_symbol = defaultdict(list)
    for candidate in candidates:
        by_symbol[(candidate.exchange, candidate.symbol)].append(candidate)

    frames = {}
    fetch_failures = {}
    start = min(c.timestamp for c in candidates) - pd.Timedelta(days=35)
    end = max(c.timestamp for c in candidates) + pd.Timedelta(days=1)
    for number, ((exchange, symbol), items) in enumerate(sorted(by_symbol.items()), 1):
        token = tokens.get((exchange, symbol))
        if token is None:
            fetch_failures[f"{exchange}:{symbol}"] = "TOKEN_NOT_FOUND"
            continue
        try:
            entry = fetch_frame(kite, token, "3minute", start.to_pydatetime(), end.to_pydatetime())
            trend = fetch_frame(kite, token, "15minute", start.to_pydatetime(), end.to_pydatetime())
            trend, entry = add_indicators(trend, entry, cfg)
            trend["ema200"] = ema(trend, 200)
            frames[(exchange, symbol)] = (entry, trend)
            print(f"FETCH {number}/{len(by_symbol)} {exchange}:{symbol} entry={len(entry)} trend={len(trend)}")
            time.sleep(0.35)
        except Exception as exc:
            fetch_failures[f"{exchange}:{symbol}"] = str(exc)

    rejection_counts = Counter()
    technical_passes = []
    audit_rows = []
    for candidate in candidates:
        data = frames.get((candidate.exchange, candidate.symbol))
        if data is None:
            rejection_counts["DATA_UNAVAILABLE"] += 1
            continue
        accepted, reason, detail, direction = technical_decision(data, candidate)
        audit_rows.append({
            "candidate": {**asdict(candidate), "timestamp": candidate.timestamp.isoformat()},
            "accepted": accepted,
            "reason": reason,
            "new_direction": direction,
            "detail": detail,
        })
        if accepted:
            technical_passes.append((candidate, direction, data[0]))
        else:
            rejection_counts[reason.split(":", 1)[0]] += 1

    admitted = []
    open_positions = []
    daily_count = Counter()
    symbol_count = Counter()
    last_losing_exit = {}
    for candidate, direction, entry_all in technical_passes:
        now = candidate.timestamp + pd.Timedelta(minutes=ENTRY_MINUTES)
        open_positions = [
            trade
            for trade in open_positions
            if pd.Timestamp(trade["exit_time"]) > now
        ]
        day = candidate.date
        symbol_key = (day, candidate.symbol)
        if daily_count[day] >= 5:
            rejection_counts["DAILY_ENTRY_CAP"] += 1
            continue
        if len(open_positions) >= 2:
            rejection_counts["MAX_OPEN_POSITIONS"] += 1
            continue
        if symbol_count[symbol_key] >= 2:
            rejection_counts["MAX_PER_SYMBOL"] += 1
            continue
        prior_loss = last_losing_exit.get(symbol_key)
        if prior_loss is not None and (now - prior_loss).total_seconds() < 1800:
            rejection_counts["POST_LOSS_COOLDOWN"] += 1
            continue

        simulated = simulate_exit(entry_all, candidate, direction)
        if simulated is None:
            rejection_counts["NO_SIZE_OR_EXIT_DATA"] += 1
            continue
        entry_time, exit_time, entry_price, qty, legs = simulated
        gross = sum(float(leg["gross_pnl"]) for leg in legs)
        costs = sum(float(leg["costs"]) for leg in legs)
        net = sum(float(leg["net_pnl"]) for leg in legs)
        trade = {
            "date": day,
            "symbol": candidate.symbol,
            "exchange": candidate.exchange,
            "candidate_time": candidate.timestamp.isoformat(),
            "entry_time": entry_time.isoformat(),
            "exit_time": exit_time.isoformat(),
            "old_direction": candidate.old_direction,
            "direction": direction,
            "entry": entry_price,
            "qty": qty,
            "gross_pnl": gross,
            "costs": costs,
            "net_pnl": net,
            "legs": legs,
        }
        admitted.append(trade)
        open_positions.append(trade)
        daily_count[day] += 1
        symbol_count[symbol_key] += 1
        if net < 0:
            last_losing_exit[symbol_key] = exit_time

    total_net = sum(row["net_pnl"] for row in admitted)
    total_gross = sum(row["gross_pnl"] for row in admitted)
    total_costs = sum(row["costs"] for row in admitted)
    wins = sum(row["net_pnl"] > 0 for row in admitted)
    losses = sum(row["net_pnl"] < 0 for row in admitted)
    days = defaultdict(list)
    for row in admitted:
        days[row["date"]].append(row)

    print("\n" + "=" * 110)
    print("CLEAN CANDLE HISTORICAL-ENTRY-COHORT REPLAY")
    print("=" * 110)
    print(f"Unique historical candidates : {len(candidates)}")
    print(f"Candle/data fetch failures   : {len(fetch_failures)} symbols")
    print(f"Passed all technical gates   : {len(technical_passes)}")
    print(f"Admitted after risk caps     : {len(admitted)}")
    print(f"Wins / losses                : {wins} / {losses}")
    print(f"Win rate                     : {(wins / len(admitted) * 100) if admitted else 0:.2f}%")
    print(f"Gross P&L                    : Rs {total_gross:+.2f}")
    print(f"Estimated costs              : Rs {total_costs:.2f}")
    print(f"NET P&L                      : Rs {total_net:+.2f}")
    print("\nPER DAY")
    for day in sorted(days):
        rows = days[day]
        day_net = sum(row["net_pnl"] for row in rows)
        day_wins = sum(row["net_pnl"] > 0 for row in rows)
        print(f"{day} trades={len(rows):2d} wins={day_wins:2d} net=Rs {day_net:+.2f}")
    print("\nTOP REJECTION STAGES")
    for reason, count in rejection_counts.most_common(15):
        print(f"{reason:45s} {count:4d}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "method": "unique retained historical-entry cohort; point-in-time Kite OHLCV enrichment",
        "limitations": [
            "not a full-universe discovery backtest",
            "intrabar stop/target ambiguity resolved conservatively stop-first",
            "entry approximated at next 3-minute candle open",
            "costs are estimates from costs.py",
        ],
        "summary": {
            "candidates": len(candidates),
            "technical_passes": len(technical_passes),
            "admitted": len(admitted),
            "wins": wins,
            "losses": losses,
            "gross_pnl": total_gross,
            "costs": total_costs,
            "net_pnl": total_net,
        },
        "fetch_failures": fetch_failures,
        "rejections": dict(rejection_counts),
        "trades": admitted,
        "audit": audit_rows,
    }, default=str, indent=2), encoding="utf-8")
    print(f"\nDETAIL={output}")


if __name__ == "__main__":
    main()
