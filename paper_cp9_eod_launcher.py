#!/usr/bin/env python3
"""PAPER-only launcher for the selected CP9_MAE20 + EOD-lock candidate.

This wrapper deliberately leaves paper_50pct_risk_launcher.py unchanged and
layers only the research-selected CP9 policy on top of it:

- current 3-minute PAPER entry policy remains unchanged;
- one-shot CP9 checkpoint at first position check >=9 minutes;
- if MAE-from-entry <= -0.20% and current P/L <0, exit remaining position;
- lock that symbol from new PAPER entries for the rest of the IST day;
- native emergency/target/hybrid -> CP9 -> MAE/adverse -> MFE/time precedence;
- sticky daily-loss and aggregate-risk guard remain active.

No LIVE behavior is changed.
"""
from __future__ import annotations

import logging

import config as cfg
import paper_50pct_risk_launcher as base_launcher

logger = logging.getLogger("paper_cp9_eod_launcher")

PAPER_CP9_EOD_ENABLED = True
PAPER_CP9_CHECKPOINT_MINUTES = 9.0
PAPER_CP9_MAE_THRESHOLD_PCT = -0.20


def apply_cp9_overrides() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: CP9 EOD launcher requires PAPER_TRADING=True")
    cfg.PAPER_CP9_EOD_ENABLED = PAPER_CP9_EOD_ENABLED
    cfg.PAPER_CP9_CHECKPOINT_MINUTES = PAPER_CP9_CHECKPOINT_MINUTES
    cfg.PAPER_CP9_MAE_THRESHOLD_PCT = PAPER_CP9_MAE_THRESHOLD_PCT


def install_stack_hooks(strategy_stack, cp9_module) -> None:
    """Inject CP9 entry/exit layers without changing the base launcher file."""
    original_two_indicator = strategy_stack.base.install_two_indicator_patch
    if not getattr(original_two_indicator, "_cp9_entry_hooked", False):
        def install_two_indicator_then_cp9_entry():
            original_two_indicator()
            cp9_module.install_cp9_eod_entry_guard(strategy_stack.base)

        install_two_indicator_then_cp9_entry._cp9_entry_hooked = True
        strategy_stack.base.install_two_indicator_patch = install_two_indicator_then_cp9_entry

    original_mae_install = strategy_stack.install_mae_adverse_exit_patch
    if not getattr(original_mae_install, "_cp9_exit_hooked", False):
        def install_cp9_then_mae(trading_main):
            # CP9 wraps native first. MAE then wraps CP9. MFE/time is installed
            # by strategy_stack.main outside MAE, yielding native -> CP9 -> MAE -> MFE.
            cp9_module.install_cp9_eod_exit_patch(trading_main)
            original_mae_install(trading_main)

        install_cp9_then_mae._cp9_exit_hooked = True
        strategy_stack.install_mae_adverse_exit_patch = install_cp9_then_mae


def main() -> None:
    base_launcher.apply_paper_risk_overrides()
    apply_cp9_overrides()
    base_launcher.install_paper_emergency_stop_override()
    base_launcher.install_direction_only_adx_policy()

    from paper_daily_risk_guard import install_paper_daily_risk_guard
    install_paper_daily_risk_guard()

    import paper_cp9_eod_guard as cp9
    import paper_mae_mfe_launcher as strategy_stack

    install_stack_hooks(strategy_stack, cp9)

    logger.warning(
        "PAPER CP9 EOD POLICY ACTIVE: >=%.0fm one-shot checkpoint; MAE<=%.2f%% AND current<0 -> exit + rest-of-day symbol lock",
        PAPER_CP9_CHECKPOINT_MINUTES,
        PAPER_CP9_MAE_THRESHOLD_PCT,
    )
    strategy_stack.main()


if __name__ == "__main__":
    main()
