#!/usr/bin/env python3
"""Compatibility entry point for the canonical MATMON preopen selector.

The selector implementation lives in matmon_directional_watchlist.py so the
preopen job cannot silently fall back to the older movement/volatility ranking.
"""
from matmon_directional_watchlist import main

if __name__ == "__main__":
    main()
