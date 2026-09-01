#!/usr/bin/env python3
"""Measure post-entry MFE/MAE and target expectancy for chart-pattern trades.

Reads the JSON produced by chart_pattern_replay.py, re-fetches point-in-time
3-minute Kite candles, and performs no broker/order writes.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from auth import get_kite_client

try:
    from costs import net_pnl_for_trade
except ImportError:
    net_pnl_for_trade = None


IST = ZoneInfo("Asia/Kolkata")
TARGETS = (0.50, 0.70, 1.00, 1.50, 2.00)
FIXED_STOP_PCT = 0.45


def fetch_frame(kite, token, start, end):
    error = None
    for attempt in range(3):
        try:
            rows = kite.historical_data(token, start, end, "3minute")
            frame = pd.DataFrame(rows)
            if frame.empty:
                return frame
            frame["date"] = pd.to_datetime(frame["date"])
            if frame["date"].dt.tz is None:
                frame["date"] = frame["date"].dt.tz_localize(IST)
            else:
                frame["date"] = frame["date"].dt.tz_convert(IST)
            for col in ("open", "high", "low", "close", "volume"):
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            return frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        except Exception as exc:
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Kite historical_data failed: {error}")


def trade_costs(direction, qty, entry, exit_price):
    if net_pnl_for_trade is not None:
        return net_pnl_for_trade(direction, qty, entry, exit_price)
    gross = (exit_price - entry) * qty * (1 if direction == "BUY" else -1)
    costs = (entry + exit_price) * qty * 0.00055
    return {"gross_pnl": gross, "costs": costs, "net_pnl": gross - costs}


def session_bars(frame, entry_time):
    entry_time = pd.Timestamp(entry_time)
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize(IST)
    else:
        entry_time = entry_time.tz_convert(IST)
    return frame.loc[
        (frame["date"] >= entry_time)
        & (frame["date"].dt.date == entry_time.date())
        & (frame["date"].dt.time <= clock_time(15, 9))
    ].copy()


def excursions(direction, entry, bars):
    if direction == "BUY":
        favourable = (bars["high"] - entry) / entry * 100
        adverse = (bars["low"] - entry) / entry * 100
    else:
        favourable = (entry - bars["low"]) / entry * 100
        adverse = (entry - bars["high"]) / entry * 100
    mfe_index = favourable.idxmax()
    mae_index = adverse.idxmin()
    return {
        "mfe_pct": max(0.0, float(favourable.loc[mfe_index])),
        "mae_pct": min(0.0, float(adverse.loc[mae_index])),
        "mfe_time": bars.loc[mfe_index, "date"].isoformat(),
        "mae_time": bars.loc[mae_index, "date"].isoformat(),
    }


def simulate_target(direction, qty, entry, bars, target_pct):
    sign = 1 if direction == "BUY" else -1
    stop = entry * (1 - sign * FIXED_STOP_PCT / 100)
    target = entry * (1 + sign * target_pct / 100)
    exit_price = float(bars.iloc[-1]["close"])
    reason = "EOD"
    exit_time = bars.iloc[-1]["date"]
    for _, bar in bars.iterrows():
        stop_hit = float(bar["low"]) <= stop if direction == "BUY" else float(bar["high"]) >= stop
        target_hit = float(bar["high"]) >= target if direction == "BUY" else float(bar["low"]) <= target
        if stop_hit:  # Conservative when both are inside one 3-minute candle.
            exit_price, reason, exit_time = stop, "STOP", bar["date"]
            break
        if target_hit:
            exit_price, reason, exit_time = target, "TARGET", bar["date"]
            break
    pnl = trade_costs(direction, qty, entry, exit_price)
    return {
        "target_pct": target_pct,
        "exit": exit_price,
        "exit_time": exit_time.isoformat(),
        "exit_reason": reason,
        **{key: float(value) for key, value in pnl.items()},
    }


def percentile(values, q):
    return float(np.percentile(values, q)) if values else 0.0


def summarise(rows):
    mfe = [float(row["mfe_pct"]) for row in rows]
    mae = [float(row["mae_pct"]) for row in rows]
    result = {
        "trades": len(rows),
        "mfe_median_pct": percentile(mfe, 50),
        "mfe_p75_pct": percentile(mfe, 75),
        "mfe_p90_pct": percentile(mfe, 90),
        "mfe_max_pct": max(mfe, default=0.0),
        "mae_median_pct": percentile(mae, 50),
        "measured_target_hit_rate": (
            sum(row["measured_target_hit"] for row in rows) / len(rows) * 100
            if rows else 0.0
        ),
    }
    for target in TARGETS:
        key = f"target_{target:.2f}"
        trials = [row["target_trials"][key] for row in rows]
        result[key] = {
            "hit_rate": sum(trial["exit_reason"] == "TARGET" for trial in trials) / len(trials) * 100 if trials else 0.0,
            "wins": sum(trial["net_pnl"] > 0 for trial in trials),
            "losses": sum(trial["net_pnl"] < 0 for trial in trials),
            "win_rate": sum(trial["net_pnl"] > 0 for trial in trials) / len(trials) * 100 if trials else 0.0,
            "gross_pnl": sum(trial["gross_pnl"] for trial in trials),
            "costs": sum(trial["costs"] for trial in trials),
            "net_pnl": sum(trial["net_pnl"] for trial in trials),
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="runtime/chart_pattern_replay_20260810_20260814.json")
    parser.add_argument("--output", default="runtime/chart_pattern_mfe_20260810_20260814.json")
    parser.add_argument("--csv", default="runtime/chart_pattern_mfe_20260810_20260814.csv")
    args = parser.parse_args()

    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    # The fixed and structural rows often describe the same signal. Use only
    # the fixed cohort, then deduplicate exact signal identities.
    source = [row for row in report.get("trades", []) if row.get("variant") == "fixed"]
    unique = {}
    for row in source:
        key = (row.get("symbol"), row.get("pattern"), row.get("direction"), row.get("signal_time"))
        unique.setdefault(key, row)
    trades = list(unique.values())
    if not trades:
        raise SystemExit("No fixed-variant trades found in input report")

    kite = get_kite_client()
    tokens = {
        str(item.get("tradingsymbol")): int(item["instrument_token"])
        for item in kite.instruments("NSE")
        if item.get("instrument_type") == "EQ"
    }
    groups = defaultdict(list)
    for trade in trades:
        groups[(trade["symbol"], trade["entry_time"][:10])].append(trade)

    enriched, failures = [], {}
    for number, ((symbol, day), items) in enumerate(sorted(groups.items()), 1):
        token = tokens.get(symbol)
        if token is None:
            failures[f"{symbol}:{day}"] = "TOKEN_NOT_FOUND"
            continue
        start = pd.Timestamp(f"{day} 09:15", tz=IST).to_pydatetime()
        end = pd.Timestamp(f"{day} 15:30", tz=IST).to_pydatetime()
        try:
            frame = fetch_frame(kite, token, start, end)
            for trade in items:
                bars = session_bars(frame, trade["entry_time"])
                if bars.empty:
                    failures[f"{symbol}:{trade['entry_time']}"] = "NO_POST_ENTRY_BARS"
                    continue
                entry = float(trade["entry"])
                direction = trade["direction"]
                row = dict(trade)
                row.update(excursions(direction, entry, bars))
                measured_pct = float(trade.get("pattern_height") or 0.0) / entry * 100
                row["measured_target_pct"] = measured_pct
                row["measured_target_hit"] = bool(row["mfe_pct"] >= measured_pct) if measured_pct > 0 else False
                row["target_trials"] = {}
                for target in TARGETS:
                    key = f"target_{target:.2f}"
                    row["target_trials"][key] = simulate_target(
                        direction, int(trade["qty"]), entry, bars, target
                    )
                enriched.append(row)
            print(f"FETCH {number}/{len(groups)} NSE:{symbol} date={day} trades={len(items)}")
            time.sleep(0.35)
        except Exception as exc:
            failures[f"{symbol}:{day}"] = str(exc)

    overall = summarise(enriched)
    by_pattern = {
        pattern: summarise([row for row in enriched if row["pattern"] == pattern])
        for pattern in sorted({row["pattern"] for row in enriched})
    }
    output = {
        "method": "post-entry 3-minute MFE/MAE until 15:09 IST; no look-ahead in simulated exits",
        "source": args.input,
        "fixed_stop_pct": FIXED_STOP_PCT,
        "targets_tested_pct": TARGETS,
        "trades": len(enriched),
        "failures": failures,
        "overall": overall,
        "by_pattern": by_pattern,
        "detail": enriched,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    flat = []
    for row in enriched:
        flat.append({
            "symbol": row["symbol"],
            "pattern": row["pattern"],
            "direction": row["direction"],
            "entry_time": row["entry_time"],
            "entry": row["entry"],
            "mfe_pct": row["mfe_pct"],
            "mfe_time": row["mfe_time"],
            "mae_pct": row["mae_pct"],
            "mae_time": row["mae_time"],
            "measured_target_pct": row["measured_target_pct"],
            "measured_target_hit": row["measured_target_hit"],
        })
    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(flat).to_csv(args.csv, index=False)

    print("\nPATTERN MFE ANALYSIS")
    print(f"TRADES={len(enriched)} FAILURES={len(failures)}")
    print(
        "OVERALL "
        f"MFE_MEDIAN={overall['mfe_median_pct']:.3f}% "
        f"MFE_P75={overall['mfe_p75_pct']:.3f}% "
        f"MFE_P90={overall['mfe_p90_pct']:.3f}% "
        f"MFE_MAX={overall['mfe_max_pct']:.3f}% "
        f"MAE_MEDIAN={overall['mae_median_pct']:.3f}%"
    )
    print("\nTARGET COMPARISON — OVERALL")
    for target in TARGETS:
        key = f"target_{target:.2f}"
        item = overall[key]
        print(
            f"TARGET={target:4.2f}% hit={item['hit_rate']:6.2f}% "
            f"win_rate={item['win_rate']:6.2f}% gross=Rs {item['gross_pnl']:+.2f} "
            f"costs=Rs {item['costs']:.2f} net=Rs {item['net_pnl']:+.2f}"
        )
    print("\nPER PATTERN")
    for pattern, item in by_pattern.items():
        target_net = {target: item[f"target_{target:.2f}"]["net_pnl"] for target in TARGETS}
        best_target = max(target_net, key=target_net.get)
        print(
            f"{pattern:36s} n={item['trades']:3d} "
            f"MFE50={item['mfe_median_pct']:6.3f}% "
            f"MFE75={item['mfe_p75_pct']:6.3f}% "
            f"MFE90={item['mfe_p90_pct']:6.3f}% "
            f"MAX={item['mfe_max_pct']:6.3f}% "
            f"BEST_TARGET={best_target:.2f}% NET=Rs {target_net[best_target]:+.2f}"
        )
    print(f"\nJSON={args.output}")
    print(f"CSV={args.csv}")


if __name__ == "__main__":
    main()
