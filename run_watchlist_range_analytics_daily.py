#!/usr/bin/env python3
"""
Wrapper to invoke process_watchlist_range_analytics() on a schedule.

This function existed, fully implemented, but nothing ever called it --
confirmed by grep across every .py/.sh file in this repo. The last
snapshot (watchlist_daily_range.json) was written manually on 2026-08-01
and never regenerated since. This wrapper is the missing scheduled caller.

Reads the watchlist the same way the rest of the live stack does
(user_config.json's "watchlist" key). If that's wrong for this
deployment, check user_config.json's actual structure before relying
on this.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import get_kite_client
from watchlist_range_analytics import process_watchlist_range_analytics


def main():
    config_path = Path(__file__).resolve().parent / "user_config.json"
    with open(config_path) as f:
        user_config = json.load(f)
    watchlist = user_config.get("watchlist", [])
    if not watchlist:
        print("ERROR: no watchlist found in user_config.json -- aborting, "
              "not writing an empty/wrong snapshot.")
        sys.exit(1)

    kite = get_kite_client()
    print(f"Running range analytics for {len(watchlist)} symbols...")
    process_watchlist_range_analytics(watchlist, kite)
    print("Done. Snapshot written to watchlist_daily_range.json")


if __name__ == "__main__":
    main()
