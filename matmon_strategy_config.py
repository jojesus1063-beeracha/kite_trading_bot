#!/usr/bin/env python3
"""Single source of truth for the Matmon (HaElohim) STRATEGY definition.

Signal parameters and the recovered September 10 live risk/exit policy live
here. Authentication, session scheduling and broker plumbing remain in config.py.
"""
from __future__ import annotations

# The Matmon universe size: how many symbols the pre-open selector picks and
# how many the intraday scan shortlist tracks. Both must agree -- a fresh
# Top-N pre-open watchlist feeding a launcher configured for a different N
# is a configuration-drift bug, not a valid state.
MATMON_WATCHLIST_SIZE = 120

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

# Backup path for an exactly flat best-bid/best-ask sequence. Flat quotes do
# not pass alone: strong and strengthening depth plus microprice drift remain
# mandatory.
MATMON_FLAT_PRESSURE_ENABLED = True
MATMON_FLAT_PRESSURE_MIN_IMBALANCE = 0.30
MATMON_FLAT_PRESSURE_MAX_SPREAD_MULTIPLIER = 1.50
MATMON_FLAT_PRESSURE_SPREAD_ALLOWANCE_BPS = 1.0

# September 10 live policy. Changing these changes the approved setup.
MATMON_CAPITAL = 5000.0
LIVE_RISK_PER_TRADE_PCT = 2.0
LIVE_MAX_POSITION_SIZE_PCT = 50.0
LIVE_MAX_OPEN_POSITIONS = 3
LIVE_MAX_TRADES_PER_DAY = 10
LIVE_MAX_DAILY_LOSS_PCT = 0.5
LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED = False
LIVE_MAX_CONSECUTIVE_LOSSES = 3
# 1% stop and 1R/2R exits are corroborated by September 10 fills.
MATMON_STOP_LOSS_PERCENT = 1.0
MATMON_PROFIT_TARGET_PERCENT = 1.0
MATMON_HYBRID_SCALP_FRACTION = 0.5
MATMON_HYBRID_SCALP_R = 1.0
MATMON_HYBRID_RUNNER_R = 2.0
