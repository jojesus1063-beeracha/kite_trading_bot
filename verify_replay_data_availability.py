"""
Pre-flight verification for the 13-date historical reconstruction replay.

Checks, per trade_history.jsonl date, WITHOUT running any strategy logic:

1. HISTORICAL_DATA_AVAILABLE -- does Kite's historical API actually
   return usable 3-minute candle data for that date? Probed with a
   single liquid benchmark instrument (NIFTY 50 index) rather than
   assuming a fixed lookback-window number, since Zerodha's actual
   retention is not something to assert from memory.

2. WATCHLIST_RECONSTRUCTABLE -- can the actual watchlist/universe that
   was live on that date be recovered from anything actually persisted
   on this system? Checked against:
   a) any dated backup file (user_config.before_*_<date>_*.json,
      or any *_<date>_*.json matching the target date) that contains a
      watchlist/symbols array
   b) runtime/auto_watchlist/latest_watchlist.json's own recorded
      generated_at date, IF it happens to match the target date
   c) git history of user_config.json (checked directly: this file is
      NOT tracked in git in this repository, so this source is
      unavailable for every date -- included here for completeness
      and to make that explicit rather than silently absent)

   No fallback to "today's watchlist" is ever used. A date with no
   verifiable source is reported False, not approximated.

3. REPLAYABLE = HISTORICAL_DATA_AVAILABLE AND WATCHLIST_RECONSTRUCTABLE

This script performs NO trading logic, NO order simulation, and makes
NO changes to any file. It only checks availability and reports it.

Run:
    BOT_DIR=~/kite_trading_bot python3 verify_replay_data_availability.py
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BOT_DIR = Path(os.path.expanduser(os.environ.get("BOT_DIR", "~/kite_trading_bot"))).resolve()
if not BOT_DIR.exists():
    raise SystemExit(f"BOT_DIR does not exist: {BOT_DIR}")
sys.path.insert(0, str(BOT_DIR))

import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles
from market_trend import NIFTY50_TOKEN


def _dates_from_trade_history():
    path = BOT_DIR / "trade_history.jsonl"
    dates = Counter()
    if not path.exists():
        return dates
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = r.get("date") or (r.get("entry_time") or "")[:10]
            if d:
                dates[d] += 1
    return dates


def check_historical_data(kite, date_str: str) -> dict:
    """Probe Kite's historical API directly for this date -- report what
    actually comes back rather than assuming a retention window."""
    target = pd.Timestamp(date_str)
    start = target.to_pydatetime()
    end = (target + pd.Timedelta(days=1)).to_pydatetime()
    try:
        df = fetch_candles(kite, NIFTY50_TOKEN, cfg.ENTRY_TIMEFRAME,
                           from_date=start, to_date=end, trim_incomplete=False)
    except Exception as exc:
        return {"available": False, "reason": f"fetch raised: {exc}", "candle_count": 0}
    if df is None or df.empty:
        return {"available": False, "reason": "empty result from Kite historical API", "candle_count": 0}
    same_day = df[df["date"].dt.date == target.date()]
    if same_day.empty:
        return {"available": False, "reason": "no rows matched the target date in returned range", "candle_count": 0}
    return {"available": True, "reason": "OK", "candle_count": len(same_day)}


def check_watchlist_reconstructable(date_str: str) -> dict:
    """Search only for ACTUALLY PERSISTED artifacts. Never falls back to
    the current/live watchlist."""
    target_compact = date_str.replace("-", "")  # YYYYMMDD, matching backup filename convention seen tonight

    sources_checked = []

    # (a) Any dated backup file whose filename contains this exact date.
    dated_backups = glob.glob(str(BOT_DIR / f"*{target_compact}*.json"))
    matching_watchlist_backups = []
    for path in dated_backups:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        wl = data.get("watchlist") if isinstance(data, dict) else None
        if wl:
            matching_watchlist_backups.append((path, len(wl)))
    sources_checked.append(("dated_backup_files", len(dated_backups), len(matching_watchlist_backups)))

    # (b) latest_watchlist.json, only if its OWN generated_at matches this date.
    latest_path = BOT_DIR / "runtime" / "auto_watchlist" / "latest_watchlist.json"
    latest_matches = False
    if latest_path.exists():
        try:
            with open(latest_path) as f:
                data = json.load(f)
            generated_at = str(data.get("generated_at", ""))
            latest_matches = generated_at.startswith(date_str)
        except Exception:
            pass
    sources_checked.append(("latest_watchlist_json_generated_at_match", 1 if latest_path.exists() else 0, 1 if latest_matches else 0))

    # (c) git history of user_config.json -- confirmed NOT tracked in this
    # repository. Included explicitly so this source is visibly absent,
    # not silently skipped.
    sources_checked.append(("git_history_user_config_json", 0, 0))  # confirmed untracked

    reconstructable = bool(matching_watchlist_backups) or latest_matches

    return {
        "reconstructable": reconstructable,
        "sources_checked": sources_checked,
        "matching_backups": [p for p, _ in matching_watchlist_backups],
    }


def main():
    kite = get_kite_client()
    dates = _dates_from_trade_history()

    print("=" * 100)
    print("PRE-FLIGHT VERIFICATION -- HISTORICAL DATA & WATCHLIST RECONSTRUCTABILITY")
    print("=" * 100)
    print("This script performs NO trading logic and makes NO changes to any file.")
    print(f"AVAILABLE_DATES (from trade_history.jsonl): {sorted(dates.keys())}")
    print()

    results = []
    for date_str in sorted(dates.keys()):
        print(f"--- {date_str} ({dates[date_str]} trade_history rows) ---")
        hist = check_historical_data(kite, date_str)
        wl = check_watchlist_reconstructable(date_str)
        replayable = hist["available"] and wl["reconstructable"]

        print(f"  HISTORICAL_DATA_AVAILABLE: {hist['available']} "
              f"({hist['candle_count']} candles) -- {hist['reason']}")
        print(f"  WATCHLIST_RECONSTRUCTABLE: {wl['reconstructable']}")
        for source_name, checked, matched in wl["sources_checked"]:
            print(f"      source={source_name} checked={checked} matched={matched}")
        if wl["matching_backups"]:
            print(f"      matching backup files: {wl['matching_backups']}")
        print(f"  REPLAYABLE: {replayable}")
        print()

        results.append({
            "date": date_str, "trade_history_rows": dates[date_str],
            "historical_data_available": hist["available"],
            "candle_count": hist["candle_count"],
            "watchlist_reconstructable": wl["reconstructable"],
            "replayable": replayable,
        })

    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    replayable_dates = [r["date"] for r in results if r["replayable"]]
    skipped = [(r["date"], "no historical data" if not r["historical_data_available"]
                else "watchlist not reconstructable") for r in results if not r["replayable"]]

    print(f"REPLAYABLE_DATES = {replayable_dates}")
    print(f"SKIPPED_DATES = {skipped}")
    print()
    print(f"Total dates checked: {len(results)}")
    print(f"Replayable: {len(replayable_dates)}")
    print(f"Skipped: {len(skipped)}")


if __name__ == "__main__":
    main()
