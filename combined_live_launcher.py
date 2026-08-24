#!/usr/bin/env python3
"""Explicitly acknowledged live launcher for the combined validated strategy.

This module does not change user_config.json and does nothing on import. Real
orders are possible only when main() is executed with PAPER_TRADING=False and
the exact acknowledgement environment variable is present.
"""
from __future__ import annotations

import logging
import os
import runpy

import config as cfg
import entry_quality
import price_action
from directional_breakout_validator import validate_breakout as validate_directional_breakout
from paper_contrarian_launcher import (
    LIVE_ACK_ENV,
    LIVE_ACK_VALUE,
    install_two_indicator_patch,
)

logger = logging.getLogger("combined_live_launcher")

LIVE_RISK_PER_TRADE_PCT = 2.0
LIVE_MAX_OPEN_POSITIONS = 1
LIVE_MAX_TRADES_PER_DAY = 10
LIVE_MAX_EMA_DISTANCE_ATR = 2.00
LIVE_MAX_DAILY_LOSS_PCT = 0.50
LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED = False
LIVE_MAX_CONSECUTIVE_LOSSES = 3
LIVE_MAX_POSITION_SIZE_PCT = 50.0
LIVE_DEPTH_CONFIRMATION_WINDOW_SECONDS = 30.0
LIVE_DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS = 15.0
LIVE_DEPTH_CONFIRMATION_MIN_SAMPLES = 5
LIVE_DEPTH_CONFIRMATION_IMBALANCE = 0.20
LIVE_DEPTH_CONFIRMATION_PERSISTENCE = 0.70
LIVE_DEPTH_CONFIRMATION_MAX_AGE_SECONDS = 2.0
LIVE_DEPTH_CONFIRMATION_MAX_SPREAD_BPS = 5.0
LIVE_DEPTH_CONFIRMATION_SIZE_MULTIPLE = 2.0


def enforce_live_limits() -> dict:
    """Fail closed, then apply the hard caps before importing main."""
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        raise SystemExit(
            "SAFETY BLOCK: combined live launcher requires paper_trading=false"
        )
    if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
        raise SystemExit(
            f"SAFETY BLOCK: set {LIVE_ACK_ENV}={LIVE_ACK_VALUE} to acknowledge real orders"
        )
    if str(getattr(cfg, "PRODUCT", "")).upper() != "MIS":
        raise SystemExit("SAFETY BLOCK: combined live launcher requires PRODUCT=MIS")
    if float(getattr(cfg, "CAPITAL", 0.0)) <= 0:
        raise SystemExit("SAFETY BLOCK: TRADING_CAPITAL must be positive")
    if getattr(cfg, "MARKET_PROTECTION", None) is None:
        raise SystemExit("SAFETY BLOCK: MARKET_PROTECTION must be configured")

    cfg.RISK_PER_TRADE_PCT = LIVE_RISK_PER_TRADE_PCT
    cfg.MAX_OPEN_POSITIONS = LIVE_MAX_OPEN_POSITIONS
    cfg.MAX_TRADES_PER_DAY = LIVE_MAX_TRADES_PER_DAY
    cfg.MAX_DAILY_LOSS_PCT = LIVE_MAX_DAILY_LOSS_PCT
    cfg.DAILY_LOSS_KILL_SWITCH_ENABLED = LIVE_DAILY_LOSS_KILL_SWITCH_ENABLED
    cfg.MAX_CONSECUTIVE_LOSSES = LIVE_MAX_CONSECUTIVE_LOSSES
    cfg.MAX_POSITION_SIZE_PCT = LIVE_MAX_POSITION_SIZE_PCT
    cfg.ENABLE_DEPTH_CONFIRMATION_GATE = True
    cfg.DEPTH_CONFIRMATION_WINDOW_SECONDS = LIVE_DEPTH_CONFIRMATION_WINDOW_SECONDS
    cfg.DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS = LIVE_DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS
    cfg.DEPTH_CONFIRMATION_MIN_SAMPLES = LIVE_DEPTH_CONFIRMATION_MIN_SAMPLES
    cfg.DEPTH_CONFIRMATION_IMBALANCE = LIVE_DEPTH_CONFIRMATION_IMBALANCE
    cfg.DEPTH_CONFIRMATION_PERSISTENCE = LIVE_DEPTH_CONFIRMATION_PERSISTENCE
    cfg.DEPTH_CONFIRMATION_MAX_AGE_SECONDS = LIVE_DEPTH_CONFIRMATION_MAX_AGE_SECONDS
    cfg.DEPTH_CONFIRMATION_MAX_SPREAD_BPS = LIVE_DEPTH_CONFIRMATION_MAX_SPREAD_BPS
    cfg.DEPTH_CONFIRMATION_SIZE_MULTIPLE = LIVE_DEPTH_CONFIRMATION_SIZE_MULTIPLE
    cfg.PROPOSED_CLEAN_PIPELINE = True
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = 120
    cfg.CHECK_MARGIN_BEFORE_ENTRY = True
    entry_quality.MAX_EMA_DISTANCE_ATR = LIVE_MAX_EMA_DISTANCE_ATR
    cfg.MAX_ENTRY_EXTENSION_ATR = 1.55
    price_action.validate_breakout = validate_directional_breakout
    cfg.ENABLE_FIXED_TARGET = True
    cfg.ENABLE_TRAILING_STOP = False
    cfg.EXIT_IMMEDIATELY_AT_TARGET = True

    return {
        "risk_per_trade_pct": cfg.RISK_PER_TRADE_PCT,
        "max_open_positions": cfg.MAX_OPEN_POSITIONS,
        "max_trades_per_day": cfg.MAX_TRADES_PER_DAY,
        "max_ema_distance_atr": entry_quality.MAX_EMA_DISTANCE_ATR,
        "max_daily_loss_pct": cfg.MAX_DAILY_LOSS_PCT,
        "daily_loss_kill_switch_enabled": cfg.DAILY_LOSS_KILL_SWITCH_ENABLED,
        "max_consecutive_losses": cfg.MAX_CONSECUTIVE_LOSSES,
        "max_position_size_pct": cfg.MAX_POSITION_SIZE_PCT,
        "check_margin_before_entry": cfg.CHECK_MARGIN_BEFORE_ENTRY,
        "depth_confirmation_gate": cfg.ENABLE_DEPTH_CONFIRMATION_GATE,
        "depth_window_seconds": cfg.DEPTH_CONFIRMATION_WINDOW_SECONDS,
        "depth_imbalance_threshold": cfg.DEPTH_CONFIRMATION_IMBALANCE,
        "depth_persistence": cfg.DEPTH_CONFIRMATION_PERSISTENCE,
        "depth_max_spread_bps": cfg.DEPTH_CONFIRMATION_MAX_SPREAD_BPS,
    }


def main() -> None:
    limits = enforce_live_limits()
    install_two_indicator_patch(live_combined=True)
    logger.critical(
        "LIVE COMBINED REAL-ORDER MODE ACTIVE | limits=%s | "
        "Momentum/RVOL Top-120; EMA9/EMA21 raw signal; frozen market-direction policy; "
        "legacy strategy metrics observational only",
        limits,
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
