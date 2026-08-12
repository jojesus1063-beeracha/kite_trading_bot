#!/usr/bin/env python3
"""PAPER-only CP9 stack + native 5m Master Candlestick final gate.

This launcher composes the existing PAPER stack without editing the existing
paper_cp9_eod_launcher.py service path.  It is intentionally a separate
entrypoint so the VM can test it before any systemd unit is changed.

No LIVE behavior is modified.
"""
from __future__ import annotations

import logging

import config as cfg
import paper_cp9_eod_launcher as cp9_launcher

logger = logging.getLogger("paper_cp9_eod_5m_full_capital_launcher")


def _install_strategy_main_with_5m_gate(strategy_stack) -> None:
    """Replace only the PAPER stack entrypoint, preserving its exit ordering."""
    if getattr(strategy_stack.main, "_native_5m_full_capital_main", False):
        return

    def main_with_native_5m_full_capital():
        # This function is called after cp9_launcher.install_stack_hooks(), so
        # these references already include the CP9 entry and exit wrappers.
        strategy_stack.base.install_two_indicator_patch()

        import main as trading_main
        from paper_native_5m_candlestick_full_capital import install

        # Critical placement: main.py is now imported (so its local aliases can
        # be patched), but trading_main.run() has not started and no order has
        # been evaluated or placed yet.
        install(trading_main)

        # Preserve the existing CP9 -> MAE -> MFE/time composition.  The
        # install_mae reference has already been wrapped by CP9's hook.
        strategy_stack.install_mae_adverse_exit_patch(trading_main)
        strategy_stack.mfe_time.install_mfe_time_exit_patch(trading_main)

        logger.warning(
            "PAPER EXIT STACK ACTIVE WITH NATIVE-5m ENTRY GATE: "
            "native stop/target -> CP9 -> MAE adverse-trend -> MFE/time"
        )
        trading_main.run()

    main_with_native_5m_full_capital._native_5m_full_capital_main = True
    strategy_stack.main = main_with_native_5m_full_capital


def main() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: 5m full-capital launcher requires PAPER_TRADING=True")

    # Preserve the existing PAPER policy layers first.
    cp9_launcher.base_launcher.apply_paper_risk_overrides()
    cp9_launcher.apply_cp9_overrides()
    cp9_launcher.base_launcher.install_paper_emergency_stop_override()
    cp9_launcher.base_launcher.install_direction_only_adx_policy()

    from paper_daily_risk_guard import install_paper_daily_risk_guard
    install_paper_daily_risk_guard()

    import paper_cp9_eod_guard as cp9
    import paper_mae_mfe_launcher as strategy_stack

    cp9_launcher.install_stack_hooks(strategy_stack, cp9)
    _install_strategy_main_with_5m_gate(strategy_stack)

    logger.warning(
        "PAPER CP9 + NATIVE 5m FULL-CAPITAL EXPERIMENT ACTIVE: "
        "ADX policy preserved; RSI override preserved; PA hard; MA hard; "
        "Master Candlestick final gate; 100%% configured PAPER-capital allocation; "
        "aggregate daily-risk guard retained"
    )
    strategy_stack.main()


if __name__ == "__main__":
    main()
