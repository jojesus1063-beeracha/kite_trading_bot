#!/usr/bin/env python3
"""Paper-only launcher for the loss-reduction experiment.

This wrapper changes only the PAPER process. Live configuration and broker
protection are untouched.

Paper risk controls:
- risk per trade: 0.50%
- max trades per day: 30
- max daily loss: 5.0%
- max entries per symbol: 2 (enforced by paper_contrarian_launcher)
- 30-minute cooldown after a losing symbol trade
- ADX 20 <= ADX < 30 paper entry block

Paper emergency-stop model:
- The strategy's original stop remains available for position sizing/audit and
  for constructing the existing hybrid 1R/2R targets.
- After a confirmed PAPER position is fully built, its executable local stop is
  widened to a 0.75% emergency stop from the confirmed entry.
- The original strategy stop is retained as paper_strategy_stop.
- If a hybrid scalp later succeeds and moves the runner stop to breakeven, that
  breakeven stop remains active and is not overwritten.

The filename is retained because the systemd unit already points to it; the old
50% daily-loss experiment is no longer active.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import config as cfg

logger = logging.getLogger("paper_50pct_risk_launcher")

PAPER_RISK_PER_TRADE_PCT = 0.50
PAPER_MAX_TRADES_PER_DAY = 30
PAPER_MAX_DAILY_LOSS_PCT = 5.0
PAPER_EMERGENCY_STOP_PCT = 0.75
PAPER_MAX_TRADES_PER_SYMBOL = 2
PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = 30.0
PAPER_ADX_BLOCK_LOW = 20.0
PAPER_ADX_BLOCK_HIGH = 30.0

# Exit-tuning parameters shared with the downstream paper wrappers.
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

DAY_STATE_PATH = Path(__file__).resolve().parent / "day_state.json"


def apply_paper_risk_overrides() -> None:
    """Apply the loss-reduction settings only inside this PAPER process."""

    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper risk-reduction launcher requires PAPER_TRADING=True"
        )

    cfg.RISK_PER_TRADE_PCT = PAPER_RISK_PER_TRADE_PCT
    cfg.MAX_TRADES_PER_DAY = PAPER_MAX_TRADES_PER_DAY
    cfg.MAX_DAILY_LOSS_PCT = PAPER_MAX_DAILY_LOSS_PCT

    # Custom paper-only settings consumed by the launcher stack.
    cfg.PAPER_EMERGENCY_STOP_PCT = PAPER_EMERGENCY_STOP_PCT
    cfg.PAPER_MAX_TRADES_PER_SYMBOL = PAPER_MAX_TRADES_PER_SYMBOL
    cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = PAPER_LOSS_REENTRY_COOLDOWN_MINUTES
    cfg.PAPER_ADX_BLOCK_LOW = PAPER_ADX_BLOCK_LOW
    cfg.PAPER_ADX_BLOCK_HIGH = PAPER_ADX_BLOCK_HIGH
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

    cleared = False
    realized_pnl = None
    trades_taken = None

    if DAY_STATE_PATH.exists():
        try:
            state = json.loads(DAY_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read paper day state for halt re-evaluation: %s", exc)
            state = None

        if isinstance(state, dict) and state.get("date") == date.today().isoformat():
            realized_pnl = float(state.get("realized_pnl", 0.0) or 0.0)
            trades_taken = int(state.get("trades_taken", 0) or 0)
            halt_reason = str(state.get("halt_reason", "") or "")
            daily_loss_halt = bool(state.get("halted")) and halt_reason.startswith(
                "Daily loss limit"
            )

            # RiskManager halts at/through the ceiling. Only release an older
            # daily-loss halt when the persisted loss is strictly inside the
            # new 5% paper ceiling. Other halt reasons are never cleared.
            if daily_loss_halt and realized_pnl > -max_loss_amount:
                state["halted"] = False
                state["halt_reason"] = ""
                DAY_STATE_PATH.write_text(
                    json.dumps(state, indent=2) + "\n",
                    encoding="utf-8",
                )
                cleared = True

    logger.warning(
        "PAPER LOSS-REDUCTION RISK ACTIVE: risk/trade=%.2f%% max_trades/day=%s "
        "daily_loss=%.1f%% (Rs %.2f) max_per_symbol=%s cooldown=%.0fm "
        "ADX_block=[%.0f,%.0f) emergency_stop=%.2f%% | today_pnl=%s trades=%s "
        "previous_daily_loss_halt_cleared=%s",
        PAPER_RISK_PER_TRADE_PCT,
        PAPER_MAX_TRADES_PER_DAY,
        PAPER_MAX_DAILY_LOSS_PCT,
        max_loss_amount,
        PAPER_MAX_TRADES_PER_SYMBOL,
        PAPER_LOSS_REENTRY_COOLDOWN_MINUTES,
        PAPER_ADX_BLOCK_LOW,
        PAPER_ADX_BLOCK_HIGH,
        PAPER_EMERGENCY_STOP_PCT,
        realized_pnl,
        trades_taken,
        cleared,
    )


def _paper_apply_emergency_stop(position: dict) -> dict:
    """Replace only a new PAPER position's initial executable stop.

    The hybrid plan has already been constructed when this function runs, so
    its 1R/2R targets continue to use the strategy stop. A later breakeven stop
    is allowed to replace this emergency stop naturally.
    """

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
        # Backward-compatible audit name from the prior paper experiment.
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
    """Patch PAPER position construction before main.py imports builders."""

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
        "PAPER EMERGENCY STOP ACTIVE: %.2f%% from confirmed entry; strategy stop retained "
        "for audit/hybrid geometry; hybrid runner breakeven remains enabled",
        PAPER_EMERGENCY_STOP_PCT,
    )


def main() -> None:
    # Apply config values before importing any downstream paper strategy module.
    apply_paper_risk_overrides()
    install_paper_emergency_stop_override()

    import paper_mae_mfe_launcher as strategy_stack

    strategy_stack.main()


if __name__ == "__main__":
    main()
