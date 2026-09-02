#!/usr/bin/env python3
"""Read-only candle-by-candle replay of a frozen watchlist for one session.

The watchlist is loaded from a selector report and is treated as if it had
been known before the opening bell.  When that report was generated using
same-day closing data, the result is an explicitly labelled ORACLE replay and
must not be interpreted as an implementable pre-market backtest.

No orders are placed and no trading state is read or written.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

import config as cfg
from candle_eligibility import evaluate_candle_eligibility
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


@dataclass
class Candidate:
    key: str
    date: str
    symbol: str
    exchange: str
    timestamp: pd.Timestamp
    old_direction: str
    old_entry: float


def normal_ema_direction(frame: pd.DataFrame):
    if frame is None or len(frame) < 21:
        return None, None, None
    close = pd.to_numeric(frame["close"], errors="coerce")
    e9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
    e21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    if pd.isna(e9) or pd.isna(e21) or e9 == e21:
        return None, None, None
    return ("BUY" if e9 > e21 else "SELL"), float(e9), float(e21)


def configure_replay() -> None:
    """Mirror the Aug-14 PAPER launcher without mutating persisted settings."""
    cfg.PAPER_TRADING = True
    cfg.ENTRY_TIMEFRAME = "3minute"
    cfg.RISK_PER_TRADE_PCT = 0.20
    cfg.MAX_TRADES_PER_DAY = 20
    cfg.MAX_OPEN_POSITIONS = 2
    cfg.MAX_DAILY_LOSS_PCT = 5.0
    cfg.PAPER_MAX_ENTRIES_PER_DAY = 20
    cfg.PAPER_MAX_TRADES_PER_SYMBOL = 2
    cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = 30.0
    cfg.PAPER_ADX_MIN_STRENGTH = 20.0
    cfg.PAPER_BUY_MIN_ADX = 25.0
    cfg.PAPER_SELL_MIN_ADX = 20.0
    cfg.PAPER_CANDLE_VOLUME_LOOKBACK = 20
    cfg.PAPER_CANDLE_MIN_VOLUME_RATIO = 1.2
    cfg.PAPER_CANDLE_REQUIRED_CONFIRMATIONS = 2
    cfg.PAPER_REQUIRE_VALIDATED_BREAKOUT = False
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
    cfg.ENABLE_CONFIRMATION_QUALITY_FILTER = True
    cfg.ENABLE_VOLUME_ACCELERATION_FILTER = True
    cfg.ENABLE_PRICE_ACTION = True
    cfg.PAPER_PRICE_ACTION_OBSERVATIONAL = True


def point_in_time_frames(data, candidate):
    entry_all, trend_all = data
    decision_time = candidate.timestamp + pd.Timedelta(minutes=ENTRY_MINUTES)
    entry = entry_all.loc[entry_all["date"] <= candidate.timestamp].copy()
    trend = trend_all.loc[
        trend_all["date"] + pd.Timedelta(minutes=15) <= decision_time
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
    breakout = (pa_detail or {}).get("breakout_validation")
    gate = evaluate_candle_eligibility(
        entry,
        trend.tail(1),
        direction,
        cfg,
        now=decision_time + pd.Timedelta(seconds=12),
        price_action_score=pa_score,
        breakout_validation=breakout,
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
    detail["ema200_watchlist"] = {
        "eligibility": eligibility,
        **eligibility_detail,
    }
    if cfg.PAPER_REQUIRE_EMA200_ALIGNMENT and eligibility != direction:
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
        reason="pre-open frozen-watchlist replay",
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


def session_candidates(frame, session_date, symbol, exchange):
    day = pd.Timestamp(session_date).date()
    start = pd.Timestamp(f"{session_date} {getattr(cfg, 'NO_ENTRY_BEFORE', '09:25')}", tz=IST)
    end = pd.Timestamp(f"{session_date} {getattr(cfg, 'NO_ENTRY_AFTER', '15:00')}", tz=IST)
    rows = frame.loc[
        (frame["date"].dt.date == day)
        & (frame["date"] >= start)
        & (frame["date"] <= end)
    ]
    return [
        Candidate(
            key=f"{session_date}|{symbol}|{row.date.isoformat()}",
            date=session_date,
            symbol=symbol,
            exchange=exchange,
            timestamp=row.date,
            old_direction="",
            old_entry=float(row.close),
        )
        for row in rows.itertuples(index=False)
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--watchlist-report", required=True)
    parser.add_argument(
        "--output", default="runtime/replay_preopen_static_watchlist_day.json"
    )
    parser.add_argument(
        "--universe-label",
        choices=("oracle_same_day", "genuine_preopen"),
        default="oracle_same_day",
    )
    args = parser.parse_args()

    configure_replay()
    # Imported after argument parsing so --help and unit tests remain usable
    # without a configured Kite SDK/session.
    from replay_clean_candle_all_days import (
        fetch_frame,
        instrument_map,
        simulate_exit,
    )
    report = json.loads(Path(args.watchlist_report).read_text(encoding="utf-8"))
    selected = report.get("selected") or []
    if not selected:
        raise SystemExit("No selected symbols found in watchlist report")

    symbols = []
    seen = set()
    for row in selected:
        symbol = str(row.get("symbol") or "").strip()
        exchange = str(row.get("exchange") or "NSE").strip()
        key = (exchange, symbol)
        if symbol and key not in seen:
            seen.add(key)
            symbols.append(key)

    print("READ_ONLY_REPLAY=True")
    print(f"SESSION_DATE={args.date}")
    print(f"UNIVERSE_LABEL={args.universe_label}")
    print(f"FROZEN_WATCHLIST_SIZE={len(symbols)}")
    if args.universe_label == "oracle_same_day":
        print("LOOKAHEAD_WARNING=Same-day final movers were not knowable before 09:00")
    print("ENTRY=next 3-minute candle open")
    print("INTRABAR_POLICY=stop before target when both occur")

    from auth import get_kite_client

    kite = get_kite_client()
    tokens = instrument_map(kite)
    session = pd.Timestamp(args.date, tz=IST)
    fetch_start = session - pd.Timedelta(days=35)
    fetch_end = session + pd.Timedelta(days=1)
    frames = {}
    failures = {}
    for number, (exchange, symbol) in enumerate(symbols, 1):
        token = tokens.get((exchange, symbol))
        if token is None:
            failures[f"{exchange}:{symbol}"] = "TOKEN_NOT_FOUND"
            continue
        try:
            entry = fetch_frame(
                kite, token, "3minute", fetch_start.to_pydatetime(), fetch_end.to_pydatetime()
            )
            time.sleep(0.35)
            trend = fetch_frame(
                kite, token, "15minute", fetch_start.to_pydatetime(), fetch_end.to_pydatetime()
            )
            time.sleep(0.35)
            trend, entry = add_indicators(trend, entry, cfg)
            trend["ema200"] = ema(trend, 200)
            frames[(exchange, symbol)] = (entry, trend)
            print(f"FETCH {number}/{len(symbols)} {exchange}:{symbol} entry={len(entry)} trend={len(trend)}")
        except Exception as exc:
            failures[f"{exchange}:{symbol}"] = str(exc)

    rank = {key: index for index, key in enumerate(symbols)}
    rejection_counts = Counter()
    audit = []
    technical_passes = []
    for exchange, symbol in symbols:
        data = frames.get((exchange, symbol))
        if data is None:
            continue
        for candidate in session_candidates(data[0], args.date, symbol, exchange):
            accepted, reason, detail, direction = technical_decision(data, candidate)
            audit.append({
                "candidate": {**asdict(candidate), "timestamp": candidate.timestamp.isoformat()},
                "accepted": accepted,
                "reason": reason,
                "direction": direction,
                "detail": detail,
            })
            if accepted:
                technical_passes.append((candidate, direction, data[0], rank[(exchange, symbol)]))
            else:
                rejection_counts[reason.split(":", 1)[0]] += 1

    technical_passes.sort(key=lambda item: (item[0].timestamp, item[3]))
    admitted = []
    open_positions = []
    symbol_count = Counter()
    last_losing_exit = {}
    daily_halt = False
    capital = float(getattr(cfg, "CAPITAL", 5000.0) or 5000.0)
    max_loss = capital * float(cfg.MAX_DAILY_LOSS_PCT) / 100.0

    for candidate, direction, entry_all, _ in technical_passes:
        now = candidate.timestamp + pd.Timedelta(minutes=ENTRY_MINUTES)
        open_positions = [trade for trade in open_positions if trade["exit_time"] > now]
        realized = sum(
            float(trade["net_pnl"])
            for trade in admitted
            if trade["exit_time"] <= now
        )
        if realized <= -max_loss:
            daily_halt = True
        if daily_halt:
            rejection_counts["DAILY_LOSS_HALT"] += 1
            continue
        if len(admitted) >= int(cfg.PAPER_MAX_ENTRIES_PER_DAY):
            rejection_counts["DAILY_ENTRY_CAP"] += 1
            continue
        if len(open_positions) >= int(cfg.MAX_OPEN_POSITIONS):
            rejection_counts["MAX_OPEN_POSITIONS"] += 1
            continue
        if any(trade["symbol"] == candidate.symbol for trade in open_positions):
            rejection_counts["SAME_SYMBOL_OPEN"] += 1
            continue
        symbol_key = (args.date, candidate.symbol)
        if symbol_count[symbol_key] >= int(cfg.PAPER_MAX_TRADES_PER_SYMBOL):
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
            "date": args.date,
            "symbol": candidate.symbol,
            "exchange": candidate.exchange,
            "signal_candle": candidate.timestamp.isoformat(),
            "entry_time": entry_time,
            "exit_time": exit_time,
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
        symbol_count[symbol_key] += 1
        if net < 0:
            last_losing_exit[symbol_key] = exit_time

    gross = sum(float(row["gross_pnl"]) for row in admitted)
    costs = sum(float(row["costs"]) for row in admitted)
    net = sum(float(row["net_pnl"]) for row in admitted)
    wins = sum(float(row["net_pnl"]) > 0 for row in admitted)
    losses = sum(float(row["net_pnl"]) < 0 for row in admitted)

    print("\n" + "=" * 108)
    print("PRE-OPEN FROZEN-WATCHLIST CANDLE REPLAY")
    print("=" * 108)
    print(f"Technical signals : {len(technical_passes)}")
    print(f"Executed trades   : {len(admitted)}")
    print(f"Wins / losses     : {wins} / {losses}")
    print(f"Win rate          : {(wins / len(admitted) * 100) if admitted else 0:.2f}%")
    print(f"Gross P&L         : Rs {gross:+.2f}")
    print(f"Estimated costs   : Rs {costs:.2f}")
    print(f"NET P&L           : Rs {net:+.2f}")
    print("\nTRADES")
    for row in admitted:
        reasons = ",".join(leg["reason"] for leg in row["legs"])
        print(
            f"{row['signal_candle']} {row['symbol']:<14} {row['direction']:<4} "
            f"qty={row['qty']:>3} entry={row['entry']:.2f} gross={row['gross_pnl']:+.2f} "
            f"costs={row['costs']:.2f} net={row['net_pnl']:+.2f} exit={reasons}"
        )
    print("\nTOP REJECTIONS")
    for reason, count in rejection_counts.most_common(15):
        print(f"{reason:45s} {count:4d}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "method": "frozen watchlist; chronological completed-candle replay",
        "universe_label": args.universe_label,
        "lookahead_warning": (
            "same-day final movers were not knowable before 09:00"
            if args.universe_label == "oracle_same_day" else None
        ),
        "limitations": [
            "market/sector alignment and broker slippage are not replayed",
            "same-candle stop/target ambiguity is resolved stop-first",
            "entry is approximated at the next 3-minute candle open",
            "candidate tie-break uses frozen watchlist order",
        ],
        "summary": {
            "watchlist_size": len(symbols),
            "fetch_failures": len(failures),
            "technical_signals": len(technical_passes),
            "executed_trades": len(admitted),
            "wins": wins,
            "losses": losses,
            "gross_pnl": gross,
            "costs": costs,
            "net_pnl": net,
        },
        "fetch_failures": failures,
        "rejections": dict(rejection_counts),
        "trades": admitted,
        "audit": audit,
    }, default=str, indent=2), encoding="utf-8")
    print(f"\nDETAIL={output}")


if __name__ == "__main__":
    main()
