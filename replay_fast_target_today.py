#!/usr/bin/env python3
"""Replay today's actual F&O PAPER entries with a fixed +target/-stop.

READ-ONLY: this script calls Kite instruments() and historical_data() only.
It does not construct PaperBroker and never calls place/modify/cancel_order.

Example:
  python replay_fast_target_today.py --date 2026-08-28 --target 15 --stop 8
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from auth import get_kite_client
from fno_bot.experiments.fast_target_scalper import FastTargetConfig, replay_long_option
from fno_bot.reporting.costs import net_pnl_for_trade

IST = ZoneInfo("Asia/Kolkata")


def load_entries(day: str):
    path = Path("fno_bot/audit_logs") / f"events_{day}.jsonl"
    if not path.exists():
        raise SystemExit(f"Missing audit file: {path}")
    rows = []
    for raw in path.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "ENTRY_FILLED":
            continue
        symbol = row.get("symbol")
        price = row.get("average_price")
        qty = row.get("filled_quantity")
        stamp = row.get("timestamp_ist")
        if symbol and price and qty and stamp:
            rows.append({"symbol": symbol, "entry_price": float(price),
                         "quantity": int(qty), "entry_time": datetime.fromisoformat(stamp)})
    return rows


def instrument_map(kite, symbols):
    wanted = set(symbols)
    found = {}
    for exchange in ("NFO", "BFO"):
        try:
            instruments = kite.instruments(exchange)
        except Exception as exc:
            print(f"WARN instruments({exchange}) failed: {exc}")
            continue
        for inst in instruments:
            sym = inst.get("tradingsymbol")
            if sym in wanted:
                found[sym] = (int(inst["instrument_token"]), exchange)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--target", type=float, default=15.0)
    ap.add_argument("--stop", type=float, default=8.0)
    ap.add_argument("--until", default="15:30", help="replay end time HH:MM IST")
    args = ap.parse_args()

    entries = load_entries(args.date)
    if not entries:
        raise SystemExit(f"No ENTRY_FILLED records found for {args.date}")

    kite = get_kite_client()
    instruments = instrument_map(kite, [e["symbol"] for e in entries])
    hh, mm = map(int, args.until.split(":"))
    cfg = FastTargetConfig(target_points=args.target, stop_points=args.stop)

    print(f"FAST_TARGET_SCALPER REPLAY | date={args.date} target=+{args.target} stop=-{args.stop}")
    print("Conservative rule: if target and stop occur in the same 1m candle, STOP wins.\n")

    totals = {"gross": 0.0, "costs": 0.0, "net": 0.0}
    for e in entries:
        sym = e["symbol"]
        if sym not in instruments:
            print(f"{sym}: SKIP - instrument not found in NFO/BFO")
            continue
        token, exchange = instruments[sym]
        start = e["entry_time"].astimezone(IST)
        end = datetime.combine(start.date(), time(hh, mm), tzinfo=IST)
        candles = kite.historical_data(token, start, end, "minute", continuous=False, oi=True)
        # The first historical candle contains time before a seconds-level entry.
        # Drop it to avoid claiming a target/stop that may have happened before the fill.
        candles = [c for c in candles if c.get("date") and c["date"].astimezone(IST).replace(second=0, microsecond=0)
                   > start.replace(second=0, microsecond=0)]
        result = replay_long_option(e["entry_price"], candles, cfg)

        if result.exit_price is None:
            last = float(candles[-1]["close"]) if candles else e["entry_price"]
            pnl = net_pnl_for_trade(e["quantity"], e["entry_price"], last)
            mark = f"MARK@{last:.2f}"
        else:
            pnl = net_pnl_for_trade(e["quantity"], e["entry_price"], result.exit_price)
            mark = f"EXIT@{result.exit_price:.2f}"

        totals["gross"] += pnl["gross_pnl"]
        totals["costs"] += pnl["costs"]
        totals["net"] += pnl["net_pnl"]
        print(
            f"{sym:<24} {exchange} entry={e['entry_price']:.2f} qty={e['quantity']:<5} "
            f"=> {result.outcome:<7} {mark:<14} points={result.points!s:<8} "
            f"net=Rs{pnl['net_pnl']:.2f} exit_time={result.exit_time}"
        )

    print("\nTOTAL")
    print(f"gross=Rs{totals['gross']:.2f} costs=Rs{totals['costs']:.2f} net=Rs{totals['net']:.2f}")
    print("NOTE: minute-candle replay validates strategy mechanics, not sub-second fill/slippage behavior.")


if __name__ == "__main__":
    main()
