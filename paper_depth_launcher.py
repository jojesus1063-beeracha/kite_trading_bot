#!/usr/bin/env python3
"""Paper-only mirror of the guarded combined strategy with depth gating.

This launcher deliberately refuses to start unless user_config resolves to
PAPER_TRADING=True.  It uses live Kite market data and the same EMA/market
policy plus persistent five-level depth gate, but executor.py's paper branch
prevents every broker order side effect.
"""
from __future__ import annotations

import logging
import runpy

import config as cfg
import entry_quality
import price_action
from directional_breakout_validator import validate_breakout as validate_directional_breakout
from paper_contrarian_launcher import install_two_indicator_patch

logger = logging.getLogger("paper_depth_launcher")

PAPER_RISK_PER_TRADE_PCT = 2.0
PAPER_MAX_OPEN_POSITIONS = 3
PAPER_CAPITAL = 5_000.0
PAPER_MAX_TRADES_PER_DAY = 7
PAPER_MAX_EMA_DISTANCE_ATR = 0.25
PAPER_MAX_DAILY_LOSS_PCT = 0.5
PAPER_DAILY_LOSS_KILL_SWITCH_ENABLED = False
PAPER_MAX_CONSECUTIVE_LOSSES = 0
PAPER_MAX_POSITION_SIZE_PCT = 50.0
PAPER_DEPTH_CONFIRMATION_WINDOW_SECONDS = 30.0
PAPER_DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS = 15.0
PAPER_DEPTH_CONFIRMATION_MIN_SAMPLES = 5
PAPER_DEPTH_CONFIRMATION_IMBALANCE = 0.20
PAPER_DEPTH_CONFIRMATION_PERSISTENCE = 0.70
PAPER_DEPTH_CONFIRMATION_MAX_AGE_SECONDS = 2.0
PAPER_DEPTH_CONFIRMATION_MAX_SPREAD_BPS = 5.0
PAPER_DEPTH_CONFIRMATION_SIZE_MULTIPLE = 2.0


def enforce_paper_depth_settings() -> dict:
    """Require paper mode and apply settings matching combined live mode."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper depth launcher requires paper_trading=true"
        )
    if not bool(getattr(cfg, "ENABLE_WS_CANDLES", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper depth launcher requires ENABLE_WS_CANDLES=True"
        )

    cfg.RISK_PER_TRADE_PCT = PAPER_RISK_PER_TRADE_PCT
    cfg.CAPITAL = PAPER_CAPITAL
    cfg.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
    cfg.MAX_TRADES_PER_DAY = PAPER_MAX_TRADES_PER_DAY
    cfg.MAX_DAILY_LOSS_PCT = PAPER_MAX_DAILY_LOSS_PCT
    cfg.DAILY_LOSS_KILL_SWITCH_ENABLED = PAPER_DAILY_LOSS_KILL_SWITCH_ENABLED
    cfg.MAX_CONSECUTIVE_LOSSES = PAPER_MAX_CONSECUTIVE_LOSSES
    cfg.MAX_POSITION_SIZE_PCT = PAPER_MAX_POSITION_SIZE_PCT
    cfg.CHECK_MARGIN_BEFORE_ENTRY = False
    cfg.PROPOSED_CLEAN_PIPELINE = True
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = 120
    entry_quality.MAX_EMA_DISTANCE_ATR = PAPER_MAX_EMA_DISTANCE_ATR
    cfg.ENABLE_FINAL_EMA_DISTANCE_GATE = True
    cfg.FINAL_EMA_DISTANCE_ATR_MAX = PAPER_MAX_EMA_DISTANCE_ATR
    cfg.MAX_ENTRY_EXTENSION_ATR = 1.55
    price_action.validate_breakout = validate_directional_breakout
    cfg.ENABLE_FIXED_TARGET = True
    cfg.ENABLE_TRAILING_STOP = False
    cfg.EXIT_IMMEDIATELY_AT_TARGET = True

    cfg.ENABLE_DEPTH_CONFIRMATION_GATE = True
    cfg.DEPTH_RAW_DIRECTION_ONLY = True
    cfg.DEPTH_REQUIRE_DIRECTIONAL_CONFIRMATION = False
    cfg.DEPTH_CONFIRMATION_WINDOW_SECONDS = PAPER_DEPTH_CONFIRMATION_WINDOW_SECONDS
    cfg.DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS = PAPER_DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS
    cfg.DEPTH_CONFIRMATION_MIN_SAMPLES = PAPER_DEPTH_CONFIRMATION_MIN_SAMPLES
    cfg.DEPTH_CONFIRMATION_IMBALANCE = PAPER_DEPTH_CONFIRMATION_IMBALANCE
    cfg.DEPTH_CONFIRMATION_PERSISTENCE = PAPER_DEPTH_CONFIRMATION_PERSISTENCE
    cfg.DEPTH_CONFIRMATION_MAX_AGE_SECONDS = PAPER_DEPTH_CONFIRMATION_MAX_AGE_SECONDS
    cfg.DEPTH_CONFIRMATION_MAX_SPREAD_BPS = PAPER_DEPTH_CONFIRMATION_MAX_SPREAD_BPS
    cfg.DEPTH_CONFIRMATION_SIZE_MULTIPLE = PAPER_DEPTH_CONFIRMATION_SIZE_MULTIPLE

    return {
        "paper_trading": cfg.PAPER_TRADING,
        "capital": cfg.CAPITAL,
        "risk_per_trade_pct": cfg.RISK_PER_TRADE_PCT,
        "max_open_positions": cfg.MAX_OPEN_POSITIONS,
        "max_trades_per_day": cfg.MAX_TRADES_PER_DAY,
        "daily_loss_kill_switch_enabled": cfg.DAILY_LOSS_KILL_SWITCH_ENABLED,
        "max_consecutive_losses": cfg.MAX_CONSECUTIVE_LOSSES,
        "depth_confirmation_gate": cfg.ENABLE_DEPTH_CONFIRMATION_GATE,
        "direction_policy": "RAW_EMA_NEAR_EMA9_PLUS_OPPOSITE_DEPTH_VETO",
        "depth_requires_directional_confirmation": (
            cfg.DEPTH_REQUIRE_DIRECTIONAL_CONFIRMATION
        ),
        "depth_window_seconds": cfg.DEPTH_CONFIRMATION_WINDOW_SECONDS,
        "depth_imbalance_threshold": cfg.DEPTH_CONFIRMATION_IMBALANCE,
        "depth_persistence": cfg.DEPTH_CONFIRMATION_PERSISTENCE,
        "depth_max_spread_bps": cfg.DEPTH_CONFIRMATION_MAX_SPREAD_BPS,
    }


def main() -> None:
    settings = enforce_paper_depth_settings()
    install_two_indicator_patch()
    logger.critical(
        "PAPER DEPTH MODE ACTIVE | settings=%s | NO REAL ORDERS | "
        "Top-120 paper watchlist; NO REVERSALS; EMA9/EMA21 raw side requires "
        "EMA9 distance <=0.25 ATR; persistent opposite five-level depth vetoes entry",
        settings,
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
