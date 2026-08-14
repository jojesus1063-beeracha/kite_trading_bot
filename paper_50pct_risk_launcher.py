#!/usr/bin/env python3
"""PAPER-only launcher for the current loss-control experiment.

This wrapper changes only the PAPER process. Live configuration and broker
protection remain untouched.

Current PAPER policy:
- risk per trade: 0.20%
- ADX is strength-only and never reverses EMA9/EMA21 direction
- ADX <20 or unavailable: BLOCK entry
- maximum 20 completed entries per day
- maximum 2 simultaneous positions
- maximum 2 completed entries per symbol with a 30-minute post-loss cooldown
- aggregate realized-loss + open-risk + proposed-risk guard remains binding
- same-symbol concurrent entries remain blocked by the core position model,
  because open_positions is keyed by symbol and cannot safely represent two
  independent same-symbol positions
- daily realized-loss halt: 5.0%, sticky for the trading day
- a service/process restart NEVER auto-clears a daily-loss halt
- explicit override requires PAPER_ALLOW_DAILY_HALT_CLEAR=YES
- executable PAPER emergency stop: 0.75%
- MAE/MFE settings are passed to the downstream paper exit wrappers

The strategy's 0.45% stop geometry is retained for position sizing, aggregate
open-risk estimation and hybrid 1R/2R target construction; after position
construction, the PAPER executable stop is widened to the 0.75% emergency
stop. A successful hybrid scalp may still move the runner stop to breakeven.
"""
from __future__ import annotations

import json
import logging

import config as cfg

logger = logging.getLogger("paper_50pct_risk_launcher")

PAPER_RISK_PER_TRADE_PCT = 0.20
PAPER_MAX_ENTRIES_PER_DAY = 20
PAPER_CORE_MAX_TRADES_PER_DAY = 20
PAPER_MAX_OPEN_POSITIONS = 2
PAPER_MAX_DAILY_LOSS_PCT = 5.0
PAPER_EMERGENCY_STOP_PCT = 0.75
PAPER_MAX_TRADES_PER_SYMBOL = 2
PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = 30.0
PAPER_ADX_MIN_STRENGTH = 20.0
PAPER_BUY_MIN_ADX = 25.0
PAPER_SELL_MIN_ADX = 20.0
PAPER_CANDLE_MIN_VOLUME_RATIO = 1.2
PAPER_CANDLE_REQUIRED_CONFIRMATIONS = 2
PAPER_REQUIRE_EMA200_ALIGNMENT = False
PAPER_ENABLE_COST_AWARE_GATE = True
PAPER_COST_MOVE_LOOKBACK = 14
PAPER_EXPECTED_MOVE_ATR_MULTIPLIER = 1.0
PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE = 2.0
PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS = 30.0

PAPER_MAE_MIN_AGE_MINUTES = 10.0
PAPER_MAE_THRESHOLD_PCT = -0.30
PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT = -0.15
PAPER_MAE_MAX_MFE_FAILURE_PCT = 0.30
PAPER_MAE_ADVERSE_CANDLES_REQUIRED = 3
PAPER_MFE_MIN_HOLD_MINUTES = 20.0
PAPER_MFE_MID_END_MINUTES = 40.0
PAPER_MFE_MID_THRESHOLD_PCT = 0.40
PAPER_MFE_LOCK_THRESHOLD_PCT = 0.50
PAPER_MFE_LOCK_CURRENT_PCT = 0.30
PAPER_MFE_LATE_THRESHOLD_PCT = 0.30
PAPER_MFE_GIVEBACK_PCT = 50.0
PAPER_DEAD_TRADE_MINUTES = 40.0
PAPER_DEAD_TRADE_MAX_MFE_PCT = 0.30


def apply_paper_risk_overrides() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper risk launcher requires PAPER_TRADING=True"
        )

    cfg.RISK_PER_TRADE_PCT = PAPER_RISK_PER_TRADE_PCT
    cfg.MAX_TRADES_PER_DAY = PAPER_CORE_MAX_TRADES_PER_DAY
    cfg.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
    cfg.MAX_DAILY_LOSS_PCT = PAPER_MAX_DAILY_LOSS_PCT

    cfg.PAPER_MAX_ENTRIES_PER_DAY = PAPER_MAX_ENTRIES_PER_DAY
    cfg.PAPER_MAX_TRADES_PER_SYMBOL = PAPER_MAX_TRADES_PER_SYMBOL
    cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = PAPER_LOSS_REENTRY_COOLDOWN_MINUTES
    cfg.PAPER_ADX_MIN_STRENGTH = PAPER_ADX_MIN_STRENGTH
    cfg.PAPER_BUY_MIN_ADX = PAPER_BUY_MIN_ADX
    cfg.PAPER_SELL_MIN_ADX = PAPER_SELL_MIN_ADX
    cfg.PAPER_CANDLE_MIN_VOLUME_RATIO = PAPER_CANDLE_MIN_VOLUME_RATIO
    cfg.PAPER_CANDLE_REQUIRED_CONFIRMATIONS = PAPER_CANDLE_REQUIRED_CONFIRMATIONS
    cfg.PAPER_REQUIRE_EMA200_ALIGNMENT = PAPER_REQUIRE_EMA200_ALIGNMENT
    cfg.PAPER_ENABLE_COST_AWARE_GATE = PAPER_ENABLE_COST_AWARE_GATE
    cfg.PAPER_COST_MOVE_LOOKBACK = PAPER_COST_MOVE_LOOKBACK
    cfg.PAPER_EXPECTED_MOVE_ATR_MULTIPLIER = PAPER_EXPECTED_MOVE_ATR_MULTIPLIER
    cfg.PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE = PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE
    cfg.PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS = PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS
    cfg.ENABLE_RVOL_FILTER = False
    cfg.ENABLE_200_EMA_FILTER = False
    cfg.ENABLE_EMA200_WATCHLIST = False
    cfg.PAPER_EMERGENCY_STOP_PCT = PAPER_EMERGENCY_STOP_PCT

    cfg.PAPER_MAE_MIN_AGE_MINUTES = PAPER_MAE_MIN_AGE_MINUTES
    cfg.PAPER_MAE_THRESHOLD_PCT = PAPER_MAE_THRESHOLD_PCT
    cfg.PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT = PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT
    cfg.PAPER_MAE_MAX_MFE_FAILURE_PCT = PAPER_MAE_MAX_MFE_FAILURE_PCT
    cfg.PAPER_MAE_ADVERSE_CANDLES_REQUIRED = PAPER_MAE_ADVERSE_CANDLES_REQUIRED
    cfg.PAPER_MFE_MIN_HOLD_MINUTES = PAPER_MFE_MIN_HOLD_MINUTES
    cfg.PAPER_MFE_MID_END_MINUTES = PAPER_MFE_MID_END_MINUTES
    cfg.PAPER_MFE_MID_THRESHOLD_PCT = PAPER_MFE_MID_THRESHOLD_PCT
    cfg.PAPER_MFE_LOCK_THRESHOLD_PCT = PAPER_MFE_LOCK_THRESHOLD_PCT
    cfg.PAPER_MFE_LOCK_CURRENT_PCT = PAPER_MFE_LOCK_CURRENT_PCT
    cfg.PAPER_MFE_LATE_THRESHOLD_PCT = PAPER_MFE_LATE_THRESHOLD_PCT
    cfg.PAPER_MFE_GIVEBACK_PCT = PAPER_MFE_GIVEBACK_PCT
    cfg.PAPER_DEAD_TRADE_MINUTES = PAPER_DEAD_TRADE_MINUTES
    cfg.PAPER_DEAD_TRADE_MAX_MFE_PCT = PAPER_DEAD_TRADE_MAX_MFE_PCT

    capital = float(getattr(cfg, "CAPITAL", 0.0) or 0.0)
    max_loss_amount = capital * PAPER_MAX_DAILY_LOSS_PCT / 100.0

    # SAFETY: a daily-loss halt is sticky.  There is intentionally no automatic
    # threshold-based unhalt here anymore.  The only supported same-day clear
    # path is the explicit PAPER_ALLOW_DAILY_HALT_CLEAR=YES operator override.
    from paper_daily_risk_guard import reconcile_startup_halt

    startup_halt = reconcile_startup_halt()

    logger.warning(
        "PAPER POLICY ACTIVE: risk/trade=%.2f%% ADX strength-only (<20/unavailable BLOCK), "
        "entry cap=%s, per-symbol completed-entry cap=%s, loss cooldown=%.0f min, "
        "delayed entry confirmation=%.0f sec, "
        "max distinct open=%s, daily_loss=%.1f%% (Rs %.2f), emergency_stop=%.2f%% | "
        "daily_halt_retained=%s explicit_halt_clear=%s",
        PAPER_RISK_PER_TRADE_PCT,
        PAPER_MAX_ENTRIES_PER_DAY,
        PAPER_MAX_TRADES_PER_SYMBOL,
        PAPER_LOSS_REENTRY_COOLDOWN_MINUTES,
        PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS,
        PAPER_MAX_OPEN_POSITIONS,
        PAPER_MAX_DAILY_LOSS_PCT,
        max_loss_amount,
        PAPER_EMERGENCY_STOP_PCT,
        bool(startup_halt.get("retained")),
        bool(startup_halt.get("cleared")),
    )


def _paper_apply_emergency_stop(position: dict) -> dict:
    if not isinstance(position, dict):
        return position
    if position.get("paper_emergency_stop_active"):
        return position

    direction = str(position.get("direction") or "").upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("Cannot apply paper emergency stop without BUY/SELL direction")

    entry = float(position.get("entry") or 0.0)
    if entry <= 0:
        raise ValueError("Cannot apply paper emergency stop without a valid entry price")

    strategy_stop = position.get("stop")
    if strategy_stop is not None:
        position["paper_strategy_stop"] = float(strategy_stop)
        position["paper_original_stop"] = float(strategy_stop)

    pct = float(getattr(cfg, "PAPER_EMERGENCY_STOP_PCT", PAPER_EMERGENCY_STOP_PCT))
    fraction = pct / 100.0
    emergency_stop = (
        entry * (1.0 - fraction)
        if direction == "BUY"
        else entry * (1.0 + fraction)
    )

    position["paper_initial_hard_stop_disabled"] = False
    position["paper_emergency_stop_active"] = True
    position["paper_emergency_stop_pct"] = pct
    position["paper_emergency_stop"] = emergency_stop
    position["stop"] = emergency_stop
    return position


def install_paper_emergency_stop_override() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper emergency-stop override requires PAPER_TRADING=True"
        )

    import entry_protection

    original_build_confirmed = entry_protection.build_confirmed_position
    original_build_recovered = entry_protection.build_recovered_position

    if not getattr(original_build_confirmed, "_paper_emergency_stop_wrapped", False):
        def build_confirmed_position(*args, **kwargs):
            position = original_build_confirmed(*args, **kwargs)
            return _paper_apply_emergency_stop(position)

        build_confirmed_position._paper_emergency_stop_wrapped = True
        entry_protection.build_confirmed_position = build_confirmed_position

    if not getattr(original_build_recovered, "_paper_emergency_stop_wrapped", False):
        def build_recovered_position(*args, **kwargs):
            position = original_build_recovered(*args, **kwargs)
            return _paper_apply_emergency_stop(position)

        build_recovered_position._paper_emergency_stop_wrapped = True
        entry_protection.build_recovered_position = build_recovered_position

    logger.warning(
        "PAPER EMERGENCY STOP ACTIVE: %.2f%% from confirmed entry; strategy stop retained for hybrid geometry",
        PAPER_EMERGENCY_STOP_PCT,
    )


def install_direction_only_adx_policy() -> None:
    """Keep ADX as a fail-closed strength gate; never patch EMA direction."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: ADX paper policy requires PAPER_TRADING=True")

    cfg.PAPER_ADX_MIN_STRENGTH = PAPER_ADX_MIN_STRENGTH
    cfg.PAPER_BUY_MIN_ADX = PAPER_BUY_MIN_ADX
    cfg.PAPER_SELL_MIN_ADX = PAPER_SELL_MIN_ADX
    logger.warning(
        "PAPER ADX STRENGTH POLICY ACTIVE: missing/<20 BLOCK; BUY requires >=25; "
        "SELL requires >=20; EMA direction is never reversed"
    )


def main() -> None:
    apply_paper_risk_overrides()
    install_paper_emergency_stop_override()
    install_direction_only_adx_policy()

    # Install before paper_mae_mfe_launcher imports main.py.  This patches only
    # the current PAPER process's RiskManager methods and executor entry callable.
    from paper_daily_risk_guard import install_paper_daily_risk_guard

    install_paper_daily_risk_guard()

    # The clean base launcher enforces per-symbol count and post-loss cooldown;
    # paper_daily_risk_guard independently enforces daily loss/open-risk safety.
    import paper_mae_mfe_launcher as strategy_stack
    strategy_stack.main()


if __name__ == "__main__":
    main()
