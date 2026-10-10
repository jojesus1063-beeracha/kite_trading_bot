#!/usr/bin/env python3
"""The sole MATMON_HAELOHIM live trading launcher.

Installs the recovered September 10 entry policy and fixed execution limits.
Requires live configuration and explicit acknowledgement before execution.
Broker order verification, reconciliation and protective exits use the shared engine.
"""
from __future__ import annotations

import logging
import os
import runpy

import config as cfg
import matmon_strategy_config as strategy_def
import matmon_policy as paper_matmon
from matmon_safety import LIVE_ACK_ENV, LIVE_ACK_VALUE
from matmon_policy import install_matmon_hooks

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
LIVE_RISK_PER_TRADE_PCT = strategy_def.LIVE_RISK_PER_TRADE_PCT
LIVE_MAX_POSITION_SIZE_PCT = strategy_def.LIVE_MAX_POSITION_SIZE_PCT
LIVE_MAX_OPEN_POSITIONS = strategy_def.LIVE_MAX_OPEN_POSITIONS
LIVE_MAX_TRADES_PER_DAY = strategy_def.LIVE_MAX_TRADES_PER_DAY
LIVE_MAX_DAILY_LOSS_PCT = strategy_def.LIVE_MAX_DAILY_LOSS_PCT
LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED = strategy_def.LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED
LIVE_MAX_CONSECUTIVE_LOSSES = strategy_def.LIVE_MAX_CONSECUTIVE_LOSSES


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
    cfg.WS_QUOTE_DEPTH_ONLY = True
    cfg.WS_ENABLE_CANDLE_SHADOW_COMPARISON = False
    cfg.WS_ENABLE_INDICATOR_SHADOW_COMPARISON = False
    cfg.MATMON_ENABLE_DASHBOARD_OBSERVATION = False
    cfg.MATMON_ENABLE_TICK_DEADLINE_SHADOW = False
    cfg.MATMON_ENABLE_VALUE_SATURATION_SHADOW = False
    cfg.MATMON_ENABLE_POST_DI_SHADOW = False
    cfg.ENABLE_EQUITY_SOCKET_SHADOW = False
    cfg.SOCKET_SHADOW_RECORD_RAW_TICKS = False
    cfg.MATMON_MODE = True
    cfg.CAPITAL = strategy_def.MATMON_CAPITAL
    cfg.STRATEGY_IDENTITY = "MATMON_HAELOHIM"
    cfg.STOP_LOSS_PERCENT = strategy_def.MATMON_STOP_LOSS_PERCENT
    cfg.PROFIT_TARGET_PERCENT = strategy_def.MATMON_PROFIT_TARGET_PERCENT
    cfg.ENABLE_FIXED_TARGET = True
    cfg.ENABLE_HYBRID_EXIT = True
    cfg.HYBRID_SCALP_FRACTION = strategy_def.MATMON_HYBRID_SCALP_FRACTION
    cfg.HYBRID_SCALP_R = strategy_def.MATMON_HYBRID_SCALP_R
    cfg.HYBRID_RUNNER_R = strategy_def.MATMON_HYBRID_RUNNER_R
    cfg.ENABLE_TRAILING_STOP = False
    cfg.MATMON_FINAL_QUOTE_GUARD = True
    cfg.MATMON_EMA_FAST = strategy_def.MATMON_EMA_FAST
    cfg.MATMON_EMA_SLOW = strategy_def.MATMON_EMA_SLOW
    cfg.MATMON_DI_PERIOD = strategy_def.MATMON_DI_PERIOD
    cfg.MATMON_QUOTE_WINDOW_SECONDS = strategy_def.MATMON_QUOTE_WINDOW_SECONDS
    cfg.MATMON_QUOTE_MAX_AGE_SECONDS = strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS
    cfg.MATMON_FLAT_PRESSURE_ENABLED = strategy_def.MATMON_FLAT_PRESSURE_ENABLED
    cfg.MATMON_FLAT_PRESSURE_MIN_IMBALANCE = strategy_def.MATMON_FLAT_PRESSURE_MIN_IMBALANCE
    cfg.MATMON_FLAT_PRESSURE_MAX_SPREAD_MULTIPLIER = strategy_def.MATMON_FLAT_PRESSURE_MAX_SPREAD_MULTIPLIER
    cfg.MATMON_FLAT_PRESSURE_SPREAD_ALLOWANCE_BPS = strategy_def.MATMON_FLAT_PRESSURE_SPREAD_ALLOWANCE_BPS
    cfg.ENTRY_TIMEFRAME = strategy_def.MATMON_ENTRY_TIMEFRAME
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = strategy_def.MATMON_WATCHLIST_SIZE
    cfg.CHECK_MARGIN_BEFORE_ENTRY = True
    cfg.HYBRID_MOVE_STOP_TO_BREAKEVEN = True
    cfg.MATMON_PREFETCH_WORKERS = 3
    cfg.MATMON_IMMEDIATE_BATCH_CONFIRMATION = True
    cfg.MATMON_SCAN_BATCH_SIZE = 12
    cfg.MATMON_PULLBACK_WAIT_ENABLED = True
    cfg.MATMON_PULLBACK_WAIT_SECONDS = 30.0
    cfg.MATMON_PULLBACK_ENTRY_BAND_PCT = 0.10
    cfg.MATMON_PULLBACK_REVERSAL_PCT = 0.15
    cfg.MATMON_PULLBACK_MAX_QUOTE_AGE_SECONDS = 1.0
    cfg.MATMON_PULLBACK_MAX_SPREAD_MULTIPLIER = 1.5

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
        "runner_stop_after_scalp": "BREAKEVEN",
        "slippage_pullback_wait_seconds": cfg.MATMON_PULLBACK_WAIT_SECONDS,
        "pullback_entry_band_pct": cfg.MATMON_PULLBACK_ENTRY_BAND_PCT,
    }


def main() -> None:
    limits = enforce_live_limits()
    install_matmon_hooks()
    logger.critical(
        "LIVE REAL-MONEY MODE ACTIVE | paper_trading=False | limits=%s | "
        "REST INPUT=3m EMA3/15 -> DI(14) -> first 3 valid post-DI ticks "
        "within 3s, strict CLEAN OR flat-with-pressure -> "
        "LTP velocity -> weighted-5 direction -> weighted-5 strengthening | "
        "adverse slippage only: wait up to 30s for executable-price pullback, "
        "then restart full 3s confirmation | "
        "incremental scan: 12-symbol batches, 3 prefetch workers, "
        "serialized confirmation after each batch | "
        "WS quote/depth-only; research observers off | legacy entry vetoes off",
        limits,
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
