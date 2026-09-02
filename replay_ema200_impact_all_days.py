#!/usr/bin/env python3
"""Read-only EMA200 impact analysis for retained historical entry candidates.

Every candidate is simulated with the same normal EMA9/EMA21 direction,
next-3-minute-open entry, position sizing, exit stack and estimated costs.
Candidates are grouped only by completed 15-minute EMA200 alignment.

This isolates EMA200 association inside the retained historical-entry cohort.
It does not discover signals that the historical bot never selected and does
not place orders or modify trading state.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

import config as cfg
import replay_clean_candle_all_days as replay
from auth import get_kite_client


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def summarize(rows: list[dict]) -> dict:
    wins = [row for row in rows if row["net_pnl"] > 0]
    losses = [row for row in rows if row["net_pnl"] < 0]
    gross = sum(float(row["gross_pnl"]) for row in rows)
    costs = sum(float(row["costs"]) for row in rows)
    net = sum(float(row["net_pnl"]) for row in rows)
    positive = sum(float(row["net_pnl"]) for row in wins)
    negative = abs(sum(float(row["net_pnl"]) for row in losses))

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["candidate_time"]):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(rows) * 100.0) if rows else 0.0,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": net,
        "average_net_pnl": (net / len(rows)) if rows else 0.0,
        "profit_factor": (positive / negative) if negative > 0 else None,
        "max_drawdown": max_drawdown,
    }


def print_summary(label: str, stats: dict) -> None:
    profit_factor = stats["profit_factor"]
    pf_text = "NA" if profit_factor is None else f"{profit_factor:.3f}"
    print(
        f"{label:22s} "
        f"trades={stats['trades']:3d} "
        f"wins={stats['wins']:3d} "
        f"losses={stats['losses']:3d} "
        f"win_rate={stats['win_rate_pct']:6.2f}% "
        f"gross=Rs {stats['gross_pnl']:+9.2f} "
        f"costs=Rs {stats['costs']:7.2f} "
        f"net=Rs {stats['net_pnl']:+9.2f} "
        f"avg=Rs {stats['average_net_pnl']:+7.2f} "
        f"PF={pf_text:>6s} "
        f"maxDD=Rs {stats['max_drawdown']:.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default="trade_history.jsonl")
    parser.add_argument(
        "--output",
        default="runtime/replay_ema200_impact_all_days.json",
    )
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    args = parser.parse_args()

    replay.configure_replay()
    candidates = replay.load_candidates(Path(args.history))
    if args.from_date:
        candidates = [item for item in candidates if item.date >= args.from_date]
    if args.to_date:
        candidates = [item for item in candidates if item.date <= args.to_date]
    if not candidates:
        raise SystemExit("No unique historical entries found")

    print("READ_ONLY_REPLAY=True")
    print("ANALYSIS=EMA200_ALIGNED_VS_MISALIGNED")
    print("TECHNICAL_ENTRY_FILTERS=NOT_APPLIED_TO_ISOLATE_EMA200")
    print("RISK_CAPS=NOT_APPLIED_TO_ISOLATE_EMA200")
    print(f"CANDIDATES={len(candidates)} DATES={candidates[0].date}..{candidates[-1].date}")
    print("IMPORTANT=Retained historical-entry cohort; not full-universe discovery")

    kite = get_kite_client()
    tokens = replay.instrument_map(kite)
    by_symbol = defaultdict(list)
    for candidate in candidates:
        by_symbol[(candidate.exchange, candidate.symbol)].append(candidate)

    frames = {}
    fetch_failures = {}
    start = min(item.timestamp for item in candidates) - pd.Timedelta(days=35)
    end = max(item.timestamp for item in candidates) + pd.Timedelta(days=1)

    for number, ((exchange, symbol), items) in enumerate(
        sorted(by_symbol.items()), 1
    ):
        token = tokens.get((exchange, symbol))
        if token is None:
            fetch_failures[f"{exchange}:{symbol}"] = "TOKEN_NOT_FOUND"
            continue
        try:
            entry = replay.fetch_frame(
                kite,
                token,
                "3minute",
                start.to_pydatetime(),
                end.to_pydatetime(),
            )
            trend = replay.fetch_frame(
                kite,
                token,
                "15minute",
                start.to_pydatetime(),
                end.to_pydatetime(),
            )
            trend, entry = replay.add_indicators(trend, entry, cfg)
            trend["ema200"] = replay.ema(trend, 200)
            frames[(exchange, symbol)] = (entry, trend)
            print(
                f"FETCH {number}/{len(by_symbol)} "
                f"{exchange}:{symbol} entry={len(entry)} trend={len(trend)}"
            )
            time.sleep(0.35)
        except Exception as exc:
            fetch_failures[f"{exchange}:{symbol}"] = str(exc)

    rows = []
    unavailable = []
    no_exit = []
    for candidate in candidates:
        data = frames.get((candidate.exchange, candidate.symbol))
        if data is None:
            unavailable.append({
                "symbol": candidate.symbol,
                "candidate_time": candidate.timestamp.isoformat(),
                "reason": "DATA_UNAVAILABLE",
            })
            continue

        entry, trend, decision_time = replay.point_in_time_frames(
            data, candidate
        )
        if (
            entry.empty
            or trend.empty
            or entry.iloc[-1]["date"] != candidate.timestamp
        ):
            unavailable.append({
                "symbol": candidate.symbol,
                "candidate_time": candidate.timestamp.isoformat(),
                "reason": "MISSING_POINT_IN_TIME_CANDLE",
            })
            continue

        direction, ema9, ema21 = replay.normal_ema_direction(entry)
        trend_close = _number(trend.iloc[-1].get("close"))
        ema200 = _number(trend.iloc[-1].get("ema200"))
        if direction is None or trend_close is None or ema200 is None:
            unavailable.append({
                "symbol": candidate.symbol,
                "candidate_time": candidate.timestamp.isoformat(),
                "reason": "EMA_DIRECTION_OR_EMA200_UNAVAILABLE",
            })
            continue

        aligned = (
            direction == "BUY" and trend_close > ema200
        ) or (
            direction == "SELL" and trend_close < ema200
        )

        simulated = replay.simulate_exit(data[0], candidate, direction)
        if simulated is None:
            no_exit.append({
                "symbol": candidate.symbol,
                "candidate_time": candidate.timestamp.isoformat(),
                "reason": "NO_SIZE_OR_EXIT_DATA",
            })
            continue

        entry_time, exit_time, entry_price, qty, legs = simulated
        rows.append({
            "date": candidate.date,
            "symbol": candidate.symbol,
            "exchange": candidate.exchange,
            "candidate_time": candidate.timestamp.isoformat(),
            "decision_time": decision_time.isoformat(),
            "entry_time": entry_time.isoformat(),
            "exit_time": exit_time.isoformat(),
            "old_direction": candidate.old_direction,
            "direction": direction,
            "ema9": ema9,
            "ema21": ema21,
            "trend_close": trend_close,
            "ema200": ema200,
            "ema200_aligned": bool(aligned),
            "entry": entry_price,
            "qty": qty,
            "gross_pnl": sum(float(leg["gross_pnl"]) for leg in legs),
            "costs": sum(float(leg["costs"]) for leg in legs),
            "net_pnl": sum(float(leg["net_pnl"]) for leg in legs),
            "legs": legs,
        })

    aligned_rows = [row for row in rows if row["ema200_aligned"]]
    misaligned_rows = [row for row in rows if not row["ema200_aligned"]]

    groups = {
        "all": summarize(rows),
        "ema200_aligned": summarize(aligned_rows),
        "ema200_misaligned": summarize(misaligned_rows),
        "aligned_buy": summarize([
            row for row in aligned_rows if row["direction"] == "BUY"
        ]),
        "aligned_sell": summarize([
            row for row in aligned_rows if row["direction"] == "SELL"
        ]),
        "misaligned_buy": summarize([
            row for row in misaligned_rows if row["direction"] == "BUY"
        ]),
        "misaligned_sell": summarize([
            row for row in misaligned_rows if row["direction"] == "SELL"
        ]),
    }

    print("\n" + "=" * 150)
    print("EMA200 IMPACT — RETAINED HISTORICAL-ENTRY COHORT")
    print("=" * 150)
    print(f"Candidates                    : {len(candidates)}")
    print(f"Fetch failures                : {len(fetch_failures)} symbols")
    print(f"Unavailable candidate outcomes: {len(unavailable)}")
    print(f"No-size/exit outcomes         : {len(no_exit)}")
    print()
    for name in (
        "all",
        "ema200_aligned",
        "ema200_misaligned",
        "aligned_buy",
        "aligned_sell",
        "misaligned_buy",
        "misaligned_sell",
    ):
        print_summary(name, groups[name])

    aligned = groups["ema200_aligned"]
    misaligned = groups["ema200_misaligned"]
    print("\nCOMPARISON")
    print(
        "Aligned minus misaligned average net/trade: "
        f"Rs {aligned['average_net_pnl'] - misaligned['average_net_pnl']:+.2f}"
    )
    print(
        "Aligned minus misaligned win rate: "
        f"{aligned['win_rate_pct'] - misaligned['win_rate_pct']:+.2f} percentage points"
    )
    print(
        "INTERPRETATION=EMA200 is supported only if aligned candidates show "
        "better net expectancy and risk-adjusted results than misaligned candidates."
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "method": (
            "all retained historical entries grouped by point-in-time completed "
            "15-minute EMA200 alignment; same normal EMA direction and exit model"
        ),
        "limitations": [
            "retained historical-entry cohort, not full-universe discovery",
            "technical entry filters and risk admission caps intentionally omitted",
            "intrabar stop/target ambiguity resolved conservatively stop-first",
            "entry approximated at next 3-minute candle open",
            "costs are estimates from costs.py",
        ],
        "candidate_count": len(candidates),
        "fetch_failures": fetch_failures,
        "unavailable": unavailable,
        "no_exit": no_exit,
        "groups": groups,
        "trades": rows,
    }, default=str, indent=2), encoding="utf-8")
    print(f"\nDETAIL={output}")


if __name__ == "__main__":
    main()
