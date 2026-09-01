#!/usr/bin/env python3
"""Explicitly acknowledged live launcher for the Matmon (HaElohim) strategy.

This module does not change user_config.json and does nothing on import.
Real orders are possible only when main() is executed with
PAPER_TRADING=False and the exact acknowledgement environment variable is
present.

It reuses the identical Matmon entry/confirmation policy already validated
in paper mode via paper_matmon_launcher.install_matmon_hooks() -- no
strategy logic (EMA3/EMA15 direction, DI14 agreement, post-DI CLEAN quote
window, microstructure confirmation, stop-loss/target fractions) is
duplicated or altered here. Order placement, verification, reconciliation,
margin checks, daily-loss/trade/position limits, and duplicate-signal
protection all come from the same shared main.py/executor.py pipeline the
already-approved combined strategy uses in live mode -- this file only
supplies Matmon's signal hook and Matmon-specific live risk caps.
"""
from __future__ import annotations

import logging
import os
import runpy

import config as cfg
import matmon_strategy_config as strategy_def
import paper_matmon_launcher as paper_matmon
from paper_contrarian_launcher import LIVE_ACK_ENV, LIVE_ACK_VALUE
from paper_matmon_launcher import install_matmon_hooks

logger = logging.getLogger("matmon_live_launcher")

# Core Matmon signal parameters now come from matmon_strategy_config -- the
# single source of truth shared with paper mode. No independent LIVE_*
# strategy copy is kept here anymore; only risk/execution caps (below) are
# legitimately live-specific.

# Live-specific risk caps -- deliberately tighter than paper mode's
# (capital=5000, risk=2%, max_position=20%, max_open=5, max_trades=100),
# following the same pattern already established for the other approved
# strategy in combined_live_launcher.py. These bound execution risk; they do
# not change what counts as a valid entry/exit signal.
LIVE_RISK_PER_TRADE_PCT = 1.0
LIVE_MAX_POSITION_SIZE_PCT = 20.0
LIVE_MAX_OPEN_POSITIONS = 1
LIVE_MAX_TRADES_PER_DAY = 10
LIVE_MAX_DAILY_LOSS_PCT = 0.50
LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED = True
LIVE_MAX_CONSECUTIVE_LOSSES = 3


def enforce_live_limits() -> dict:
    """Fail closed, then apply the hard live caps before importing main."""
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        raise SystemExit(
            "SAFETY BLOCK: matmon live launcher requires paper_trading=false"
        )
    if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
        raise SystemExit(
            f"SAFETY BLOCK: set {LIVE_ACK_ENV}={LIVE_ACK_VALUE} to acknowledge real orders"
        )
    if str(getattr(cfg, "PRODUCT", "")).upper() != "MIS":
        raise SystemExit("SAFETY BLOCK: matmon live launcher requires PRODUCT=MIS")
    if float(getattr(cfg, "CAPITAL", 0.0)) <= 0:
        raise SystemExit("SAFETY BLOCK: TRADING_CAPITAL must be positive")
    if getattr(cfg, "MARKET_PROTECTION", None) is None:
        raise SystemExit("SAFETY BLOCK: MARKET_PROTECTION must be configured")
    if not bool(getattr(cfg, "ENABLE_WS_CANDLES", False)):
        raise SystemExit(
            "SAFETY BLOCK: matmon live launcher requires ENABLE_WS_CANDLES=True "
            "(Matmon's post-DI CLEAN quote confirmation needs live tick/depth data)"
        )

    # Matmon-specific runtime wiring -- sourced from matmon_strategy_config,
    # identical to what paper mode consumes.
    cfg.WS_CANDLE_MODE = "shadow"
    cfg.MATMON_MODE = True
    cfg.MATMON_EMA_FAST = strategy_def.MATMON_EMA_FAST
    cfg.MATMON_EMA_SLOW = strategy_def.MATMON_EMA_SLOW
    cfg.MATMON_DI_PERIOD = strategy_def.MATMON_DI_PERIOD
    cfg.MATMON_QUOTE_WINDOW_SECONDS = strategy_def.MATMON_QUOTE_WINDOW_SECONDS
    cfg.MATMON_QUOTE_MAX_AGE_SECONDS = strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS
    cfg.ENTRY_TIMEFRAME = strategy_def.MATMON_ENTRY_TIMEFRAME
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = strategy_def.MATMON_WATCHLIST_SIZE
    cfg.CHECK_MARGIN_BEFORE_ENTRY = True

    # Live risk caps.
    cfg.RISK_PER_TRADE_PCT = LIVE_RISK_PER_TRADE_PCT
    cfg.MAX_POSITION_SIZE_PCT = LIVE_MAX_POSITION_SIZE_PCT
    cfg.MAX_OPEN_POSITIONS = LIVE_MAX_OPEN_POSITIONS
    cfg.MAX_TRADES_PER_DAY = LIVE_MAX_TRADES_PER_DAY
    cfg.MAX_DAILY_LOSS_PCT = LIVE_MAX_DAILY_LOSS_PCT
    cfg.DAILY_LOSS_KILL_SWITCH_ENABLED = LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED
    cfg.MAX_CONSECUTIVE_LOSSES = LIVE_MAX_CONSECUTIVE_LOSSES

    # Same confirmation-pipeline flags paper mode validated.
    cfg.PROPOSED_CLEAN_PIPELINE = True
    cfg.ENABLE_DEPTH_CONFIRMATION_GATE = True
    cfg.DEPTH_RAW_DIRECTION_ONLY = True
    cfg.DEPTH_REQUIRE_DIRECTIONAL_CONFIRMATION = True
    cfg.PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS = 0.0

    # Keep every legacy research veto disabled, exactly as paper mode does.
    for name in paper_matmon.MATMON_FORBIDDEN_TRUE:
        setattr(cfg, name, False)

    return {
        "strategy": "MATMON_HAELOHIM",
        "capital": cfg.CAPITAL,
        "risk_per_trade_pct": cfg.RISK_PER_TRADE_PCT,
        "max_position_size_pct": cfg.MAX_POSITION_SIZE_PCT,
        "max_open_positions": cfg.MAX_OPEN_POSITIONS,
        "max_trades_per_day": cfg.MAX_TRADES_PER_DAY,
        "max_daily_loss_pct": cfg.MAX_DAILY_LOSS_PCT,
        "daily_loss_kill_switch_enabled": cfg.DAILY_LOSS_KILL_SWITCH_ENABLED,
        "max_consecutive_losses": cfg.MAX_CONSECUTIVE_LOSSES,
        "check_margin_before_entry": cfg.CHECK_MARGIN_BEFORE_ENTRY,
    }


def main() -> None:
    limits = enforce_live_limits()
    install_matmon_hooks()
    logger.critical(
        "LIVE REAL-MONEY MODE ACTIVE | paper_trading=False | limits=%s | "
        "REST INPUT=3m EMA3/15 -> DI(14) -> post-DI 3s FULL-PATH CLEAN -> "
        "LTP velocity -> weighted-5 direction -> weighted-5 strengthening | "
        "WS candles shadow-only | legacy entry vetoes off",
        limits,
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
