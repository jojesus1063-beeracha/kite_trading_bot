"""
Fail-safe interface to the append-only validation ledger.

Validation logging must never prevent scanning, position monitoring,
risk checks or order handling. Any ledger failure is reported through
the application logger and the trading pipeline continues unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from validation_log import append_validation_event


logger = logging.getLogger(__name__)


def record_validation_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    log_dir: str | Path | None = None,
) -> bool:
    """
    Record a validation event without changing trading behaviour.

    Returns True when written successfully and False when recording
    fails. Exceptions are deliberately contained.
    """

    try:
        append_validation_event(
            event_type,
            payload,
            log_dir=log_dir,
        )

        return True
    except Exception as exc:
        logger.exception(
            "Validation event recording failed "
            f"| event_type={event_type} "
            f"| error={exc}"
        )

        return False


def signal_snapshot(signal: Any) -> dict[str, Any]:
    """
    Extract stable signal fields without requiring a specific class.
    """

    if signal is None:
        return {}

    fields = (
        "symbol",
        "direction",
        "entry_price",
        "stop_loss",
        "target",
        "timestamp",
        "reason",
        "confidence",
        "market_alignment",
        "news_sentiment",
        "price_action_score",
        "entry_quality_score",
        "entry_quality_detail",
        "entry_context_score",
        "entry_context_detail",
        "relative_strength_score",
        "relative_strength_detail",
    )

    return {
        field: getattr(
            signal,
            field,
            None,
        )
        for field in fields
    }


def candidate_snapshot(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """
    Extract validation-safe ranking fields from a candidate dictionary.
    """

    fields = (
        "symbol",
        "ranking_score",
        "entry_quality_score",
        "entry_quality_detail",
        "entry_context_score",
        "entry_context_detail",
        "relative_strength_score",
        "relative_strength_detail",
    )

    snapshot = {
        field: candidate.get(field)
        for field in fields
    }

    snapshot["signal"] = signal_snapshot(
        candidate.get("signal")
    )

    return snapshot
