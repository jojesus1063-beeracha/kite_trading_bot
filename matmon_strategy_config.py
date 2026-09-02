#!/usr/bin/env python3
"""Single source of truth for the Matmon (HaElohim) STRATEGY definition.

This module intentionally holds only strategy-signal parameters -- what
counts as a valid Matmon entry candidate and how its post-DI CLEAN quote
confirmation is timed. It deliberately does NOT hold:

  * risk/execution values (capital, risk-per-trade, position/trade/loss
    caps) -- these differ between paper and live and belong to each
    launcher's own risk policy, not the strategy definition.
  * session/scheduling values (no-entry-before/after, square-off time,
    position-check interval, pre-open selector timing) -- these are
    execution-calendar concerns, not signal-generation concerns.

paper_matmon_launcher.py, matmon_live_launcher.py, matmon_live_candidate_launcher.py,
and matmon_preopen_top120.py all read from here instead of independently
redefining the same constants. Changing Matmon's strategy definition means
changing it in exactly one place.
"""
from __future__ import annotations

# The Matmon universe size: how many symbols the pre-open selector picks and
# how many the intraday scan shortlist tracks. Both must agree -- a fresh
# Top-N pre-open watchlist feeding a launcher configured for a different N
# is a configuration-drift bug, not a valid state.
MATMON_WATCHLIST_SIZE = 60

# REST 3-minute EMA fast/slow period and DI period used for the completed-
# candle direction/agreement step.
MATMON_EMA_FAST = 3
MATMON_EMA_SLOW = 15
MATMON_DI_PERIOD = 14

# Candle timeframe the EMA/DI direction step is computed on.
MATMON_ENTRY_TIMEFRAME = "3minute"

# Post-DI confirmation window: how long after DI-agreement a tick must
# arrive within (CLEAN window), and the maximum age a quote may have to
# still count as fresh.
MATMON_QUOTE_WINDOW_SECONDS = 3.0
MATMON_QUOTE_MAX_AGE_SECONDS = 2.0
