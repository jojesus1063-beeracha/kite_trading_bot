#!/usr/bin/env python3
"""Matmon live-candidate DRY-RUN launcher.

This file validates the intended production strategy path without exposing or
invoking a real-order boundary. It deliberately requires PAPER_TRADING=True,
REST-authoritative completed candles (WS_CANDLE_MODE=shadow), and contains no
broker order call.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import logging

import config as cfg
import matmon_strategy_config as strategy_def
from matmon_entry_policy import evaluate_direction
from matmon_quote_confirmation import evaluate_quote_window
from matmon_microstructure import evaluate_microstructure

logger = logging.getLogger("matmon_live_candidate_launcher")

REQUIRED_EMA_FAST = 3
REQUIRED_EMA_SLOW = 15
REQUIRED_DI_PERIOD = 14
REQUIRED_ENTRY_TIMEFRAME = "3minute"
REQUIRED_WS_CANDLE_MODE = "shadow"


@dataclass
class CandidateResult:
    accepted: bool
    direction: str | None
    reason: str
    ema_di: dict | None = None
    clean: dict | None = None
    microstructure: dict | None = None
    execution_boundary: str = "DRY_RUN_ONLY"

    def to_dict(self):
        return asdict(self)


def assert_dry_run_contract(cfg_obj=cfg):
    """Fail closed unless the production-candidate remains simulation only."""
    errors = []
    if not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
        errors.append("PAPER_TRADING must be True")
    if not bool(getattr(cfg_obj, "ENABLE_WS_CANDLES", False)):
        errors.append("ENABLE_WS_CANDLES must be True")
    if str(getattr(cfg_obj, "WS_CANDLE_MODE", REQUIRED_WS_CANDLE_MODE)).lower() != REQUIRED_WS_CANDLE_MODE:
        errors.append("WS_CANDLE_MODE must be shadow so REST candles remain authoritative")
    if str(getattr(cfg_obj, "ENTRY_TIMEFRAME", REQUIRED_ENTRY_TIMEFRAME)) != REQUIRED_ENTRY_TIMEFRAME:
        errors.append("ENTRY_TIMEFRAME must be 3minute")
    if int(getattr(cfg_obj, "MATMON_EMA_FAST", REQUIRED_EMA_FAST)) != REQUIRED_EMA_FAST:
        errors.append("MATMON_EMA_FAST must be 3")
    if int(getattr(cfg_obj, "MATMON_EMA_SLOW", REQUIRED_EMA_SLOW)) != REQUIRED_EMA_SLOW:
        errors.append("MATMON_EMA_SLOW must be 15")
    if int(getattr(cfg_obj, "MATMON_DI_PERIOD", REQUIRED_DI_PERIOD)) != REQUIRED_DI_PERIOD:
        errors.append("MATMON_DI_PERIOD must be 14")
    if not bool(getattr(cfg_obj, "CHECK_MARGIN_BEFORE_ENTRY", True)):
        errors.append("CHECK_MARGIN_BEFORE_ENTRY must be True")

    if errors:
        raise SystemExit("SAFETY BLOCK: Matmon live-candidate contract failed: " + "; ".join(errors))
    return True


def authorize_candidate(*, tick_buffer, symbol, ema3, ema15, plus_di, minus_di,
                        cfg_obj=cfg, now=None, not_before=None):
    """Run the complete Matmon strategy contract and stop at a dry-run boundary."""
    assert_dry_run_contract(cfg_obj)

    ema_di = evaluate_direction(
        ema3=ema3,
        ema15=ema15,
        plus_di=plus_di,
        minus_di=minus_di,
    )
    if not ema_di.accepted:
        return CandidateResult(False, ema_di.direction, ema_di.reason, ema_di.to_dict())

    clean = evaluate_quote_window(
        tick_buffer,
        symbol,
        ema_di.direction,
        window_seconds=float(getattr(
            cfg_obj, "MATMON_QUOTE_WINDOW_SECONDS", strategy_def.MATMON_QUOTE_WINDOW_SECONDS
        )),
        max_age_seconds=float(getattr(
            cfg_obj, "MATMON_QUOTE_MAX_AGE_SECONDS", strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS
        )),
        now=now,
        not_before=not_before,
    )
    if not clean.confirmed:
        return CandidateResult(
            False, ema_di.direction, clean.reason, ema_di.to_dict(), clean.to_dict()
        )

    micro = evaluate_microstructure(ema_di.direction, clean.ticks)
    if not micro.accepted:
        return CandidateResult(
            False,
            ema_di.direction,
            micro.reason,
            ema_di.to_dict(),
            clean.to_dict(),
            micro.to_dict(),
        )

    return CandidateResult(
        True,
        ema_di.direction,
        "MATMON_DRY_RUN_AUTHORIZED",
        ema_di.to_dict(),
        clean.to_dict(),
        micro.to_dict(),
    )


def dry_run_execution_boundary(result: CandidateResult):
    """Return audit data only. This function never submits a broker order."""
    accepted = isinstance(result, CandidateResult) and bool(result.accepted)
    payload = {
        "would_authorize": accepted,
        # Retained only for older audit/tests; this does not mean an order is submitted.
        "would_submit": accepted,
        "reason": getattr(result, "reason", "REJECTED"),
    }
    if accepted:
        payload.update(
            {
                "direction": result.direction,
                "execution_boundary": "DRY_RUN_ONLY",
            }
        )
    return payload


def main():
    assert_dry_run_contract(cfg)
    logger.critical(
        "MATMON LIVE-CANDIDATE DRY-RUN READY | REST completed 3m candles | "
        "EMA3/15 -> DI14 -> fresh post-DI 3s FULL-PATH CLEAN -> LTP velocity -> "
        "weighted-5 direction -> weighted-5 strengthening | NO REAL ORDERS"
    )


if __name__ == "__main__":
    main()
