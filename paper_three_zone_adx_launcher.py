#!/usr/bin/env python3
"""Backward-compatible PAPER launcher for the clean ADX strength policy.

The former three-zone reversal experiment is deliberately retired.  This file
remains as a compatible service entry point, but now installs the same normal
EMA9/EMA21 direction and fail-closed ADX thresholds as the clean launcher.
"""
from __future__ import annotations

import logging

import paper_50pct_risk_launcher as current

logger = logging.getLogger("paper_three_zone_adx_launcher")


def install_strict_adx_policy() -> None:
    if not bool(getattr(current.cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: clean ADX launcher requires PAPER_TRADING=True")

    current.cfg.PAPER_ADX_MIN_STRENGTH = 20.0
    current.cfg.PAPER_BUY_MIN_ADX = 25.0
    current.cfg.PAPER_SELL_MIN_ADX = 20.0
    logger.warning(
        "PAPER CLEAN ADX ACTIVE: no reversal; missing/<20 BLOCK; "
        "BUY requires >=25 and SELL requires >=20"
    )


# Preserve the existing service/launcher chain while retiring its reversal.
current.install_direction_only_adx_policy = install_strict_adx_policy


if __name__ == "__main__":
    current.main()
