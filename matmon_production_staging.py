#!/usr/bin/env python3
"""Matmon production-staging boundary with broker execution deliberately disabled.

This module preserves the verified Matmon candidate authorization path and
turns an accepted candidate into an immutable execution *intent* for auditing.
It does not import executor/protective_stop and cannot submit, modify, or cancel
broker orders. The final real-order integration remains a separate deployment
decision.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import logging

import config as cfg
from matmon_live_candidate_launcher import (
    CandidateResult,
    assert_dry_run_contract,
    authorize_candidate,
)

logger = logging.getLogger("matmon_production_staging")

EXECUTION_BOUNDARY = "BROKER_EXECUTION_DISABLED"


@dataclass(frozen=True)
class ExecutionIntent:
    symbol: str
    direction: str
    reason: str
    execution_boundary: str = EXECUTION_BOUNDARY

    def to_dict(self):
        return asdict(self)


def build_execution_intent(*, symbol: str, result: CandidateResult) -> ExecutionIntent | None:
    """Create an auditable intent only after the complete Matmon contract passes."""
    if not isinstance(result, CandidateResult) or not result.accepted or result.direction not in {"BUY", "SELL"}:
        return None
    return ExecutionIntent(
        symbol=symbol,
        direction=result.direction,
        reason=result.reason,
    )


def evaluate_for_staging(*, tick_buffer, symbol, ema3, ema15, plus_di, minus_di,
                         cfg_obj=cfg, now=None, not_before=None):
    """Run the exact verified candidate path, then stop before broker execution."""
    assert_dry_run_contract(cfg_obj)
    result = authorize_candidate(
        tick_buffer=tick_buffer,
        symbol=symbol,
        ema3=ema3,
        ema15=ema15,
        plus_di=plus_di,
        minus_di=minus_di,
        cfg_obj=cfg_obj,
        now=now,
        not_before=not_before,
    )
    return result, build_execution_intent(symbol=symbol, result=result)


def submit_execution_intent(_intent: ExecutionIntent):
    """Hard boundary: production staging must never reach a broker order API."""
    raise RuntimeError("BROKER_EXECUTION_DISABLED: Matmon production staging cannot submit orders")


def main():
    assert_dry_run_contract(cfg)
    logger.critical(
        "MATMON PRODUCTION STAGING READY | exact verified candidate authorization path | "
        "BROKER EXECUTION DISABLED | no place/modify/cancel capability"
    )


if __name__ == "__main__":
    main()
