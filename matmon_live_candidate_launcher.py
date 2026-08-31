#!/usr/bin/env python3
"""Matmon live-candidate DRY-RUN launcher.

This file exists to validate the intended live strategy path without exposing
or invoking a real-order boundary. It deliberately requires PAPER_TRADING=True
and contains no broker order call.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import logging

import config as cfg
from matmon_entry_policy import evaluate_direction
from matmon_quote_confirmation import evaluate_quote_window
from matmon_microstructure import evaluate_microstructure

logger = logging.getLogger("matmon_live_candidate_launcher")


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
    if not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: Matmon live-candidate requires PAPER_TRADING=True")
    if not bool(getattr(cfg_obj, "ENABLE_WS_CANDLES", False)):
        raise SystemExit("SAFETY BLOCK: Matmon requires MODE_FULL WebSocket data")
    return True


def authorize_candidate(*, tick_buffer, symbol, ema3, ema15, plus_di, minus_di,
                        cfg_obj=cfg, now=None):
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
        window_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_WINDOW_SECONDS", 3.0)),
        max_age_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_MAX_AGE_SECONDS", 2.0)),
        now=now,
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
    if not isinstance(result, CandidateResult) or not result.accepted:
        return {"would_submit": False, "reason": getattr(result, "reason", "REJECTED")}
    return {
        "would_submit": True,
        "direction": result.direction,
        "reason": result.reason,
        "execution_boundary": "DRY_RUN_ONLY",
    }


def main():
    assert_dry_run_contract(cfg)
    logger.critical(
        "MATMON LIVE-CANDIDATE DRY-RUN READY | EMA3/15 -> DI14 -> 3s FULL-PATH CLEAN -> "
        "LTP velocity -> weighted-5 direction -> weighted-5 strengthening | NO REAL ORDERS"
    )


if __name__ == "__main__":
    main()
