#!/usr/bin/env python3
"""Launch the normal bot in paper-only contrarian direction mode.

Safety properties:
- Refuses to run unless config.PAPER_TRADING is True.
- Does not modify strategy.py or live service behavior.
- Inverts only strategy trend classification at runtime:
    normal UP   -> strategy sees DOWN -> searches SELL
    normal DOWN -> strategy sees UP   -> searches BUY
- The existing confirmation, risk, stop, target and execution pipeline remains active.
"""

from __future__ import annotations

import logging
import runpy

import config as cfg
import strategy

logger = logging.getLogger("paper_contrarian_launcher")


def invert_trend(value):
    if value == "UP":
        return "DOWN"
    if value == "DOWN":
        return "UP"
    return value


def install_contrarian_patch() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper contrarian launcher requires PAPER_TRADING=True"
        )

    original_get_trend = strategy.get_trend
    original_evaluate = strategy.evaluate

    def contrarian_get_trend(row_15m, cfg=None, require_vwap=True):
        normal = original_get_trend(row_15m, cfg, require_vwap=require_vwap)
        return invert_trend(normal)

    def contrarian_evaluate(*args, **kwargs):
        signal = original_evaluate(*args, **kwargs)
        if signal is not None:
            signal.reason = "PAPER CONTRARIAN | " + str(signal.reason)
        return signal

    strategy.get_trend = contrarian_get_trend
    strategy.evaluate = contrarian_evaluate

    logger.warning(
        "PAPER CONTRARIAN MODE ACTIVE: normal UP -> SELL search; normal DOWN -> BUY search"
    )


def main() -> None:
    install_contrarian_patch()
    # main.py imports from the already-patched strategy module, so the normal
    # bot lifecycle, risk controls, exits, scheduler and reporting are reused.
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
