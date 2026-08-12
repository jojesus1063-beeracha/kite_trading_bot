#!/usr/bin/env python3
"""Dedicated PAPER launcher for tested 5m Master Candlestick + full cash capital.

This launcher preserves the existing CP9/MAE/MFE PAPER stack and daily aggregate
risk guard, then installs two PAPER-only entry changes:

1. Final entry timing gate = Master Candlestick Engine on the same approximate
   3m->5m geometry used by the Aug-12 replay where FORTIS simulated +Rs6.81.
2. Position sizing = up to 100% of configured PAPER cash CAPITAL per trade.

Safety properties:
- PAPER_TRADING must be True.
- MAX_OPEN_POSITIONS is forced to 1 so 100% cash cannot be allocated twice.
- no leverage is assumed or invented.
- daily-loss and aggregate-open-risk guards remain active.
- NEXT_OPEN candlestick plans fail closed until a true 5m bar-boundary execution
  path is separately implemented/tested.
- LIVE code/configuration is untouched; all changes are runtime monkey patches
  inside this dedicated process.
"""
from __future__ import annotations

import logging

import config as cfg
import paper_50pct_risk_launcher as base_launcher
import paper_cp9_eod_launcher as cp9_launcher

from paper_5m_master_full_capital import install_on_trading_main, reset_runtime_state

logger = logging.getLogger("paper_5m_master_full_capital_launcher")


def apply_tested_paper_overrides() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: full-capital master launcher requires PAPER_TRADING=True")

    # Entry experiment identity.
    cfg.PAPER_MASTER_CANDLESTICK_GATE = True
    cfg.PAPER_MASTER_CANDLESTICK_TIMEFRAME = "5minute"
    cfg.PAPER_MASTER_CANDLESTICK_SOURCE = "RESAMPLED_FROM_COMPLETED_3MINUTE"
    cfg.PAPER_MASTER_CANDLESTICK_MIN_RR = 2.0
    cfg.PAPER_MASTER_CANDLESTICK_MAX_WAIT_BARS = 2
    cfg.PAPER_MASTER_CANDLESTICK_NEXT_OPEN = "FAIL_CLOSED"

    # The Master engine supplies a geometric stop.  Do not replace it with the
    # legacy flat 0.45% stop before the aggregate-risk guard sees the proposal.
    cfg.ENABLE_FIXED_TARGET = False

    # Make 100% of CASH capital available to one PAPER trade.  One open position
    # at a time prevents double-allocation of the same capital.
    cfg.PAPER_FULL_CAPITAL_PER_TRADE = True
    cfg.PAPER_CAPITAL_FRACTION_PER_TRADE = 1.0
    cfg.MAX_POSITION_SIZE_PCT = 100.0
    cfg.MAX_OPEN_POSITIONS = 1

    # Explicitly retain these tested filters in the process configuration.  The
    # existing main/launcher stack still decides how upstream signals are formed;
    # the Master gate never reverses direction itself.
    cfg.ENABLE_PRICE_ACTION = True
    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = True

    logger.warning(
        "PAPER TEST OVERRIDES: master_tf=5m(resampled from completed 3m), minRR=2, wait=2, full_cash=100%%, max_open=1, fixed_stop_reconstruction=OFF"
    )


def main() -> None:
    # Preserve the current PAPER risk/daily-loss/ADX policy first, then layer the
    # requested experiment overrides on top.
    base_launcher.apply_paper_risk_overrides()
    apply_tested_paper_overrides()
    cp9_launcher.apply_cp9_overrides()

    # Keep the current executable PAPER emergency-stop safety layer.  The
    # aggregate-risk proposal itself is still calculated from the Master
    # geometric stop because ENABLE_FIXED_TARGET=False and entry_plan stores the
    # signal stop.  This intentionally preserves the existing PAPER safety exit
    # stack rather than weakening it while increasing capital allocation.
    base_launcher.install_paper_emergency_stop_override()
    base_launcher.install_direction_only_adx_policy()

    from paper_daily_risk_guard import install_paper_daily_risk_guard
    install_paper_daily_risk_guard()

    import paper_cp9_eod_guard as cp9
    import paper_mae_mfe_launcher as strategy_stack

    cp9_launcher.install_stack_hooks(strategy_stack, cp9)

    # Install the existing upstream PAPER strategy patch before importing
    # main.py, exactly as the current CP9 stack does.
    strategy_stack.base.install_two_indicator_patch()

    import main as trading_main

    reset_runtime_state()
    install_on_trading_main(trading_main)

    # Preserve current CP9 -> MAE -> MFE/time exit wrapping.
    strategy_stack.install_mae_adverse_exit_patch(trading_main)
    strategy_stack.mfe_time.install_mfe_time_exit_patch(trading_main)

    logger.warning(
        "PAPER 5m MASTER + FULL-CAPITAL POLICY ACTIVE | capital=Rs %.2f | max_open=%s | daily_loss=%.2f%%",
        float(getattr(cfg, "CAPITAL", 0.0) or 0.0),
        getattr(cfg, "MAX_OPEN_POSITIONS", None),
        float(getattr(cfg, "MAX_DAILY_LOSS_PCT", 0.0) or 0.0),
    )
    logger.warning(
        "PAPER EXIT STACK RETAINED: emergency stop -> CP9 -> MAE adverse-trend -> MFE/time; aggregate risk guard remains binding"
    )
    trading_main.run()


if __name__ == "__main__":
    main()
