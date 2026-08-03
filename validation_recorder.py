"""
Fail-safe interface to the append-only validation ledger.

Validation logging must never prevent scanning, position monitoring,
risk checks or order handling. Any ledger failure is reported through
the application logger and the trading pipeline continues unchanged.
"""

from __future__ import annotations

import logging
import os
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
        effective_log_dir = (
            log_dir
            or os.environ.get(
                "KITE_VALIDATION_LOG_DIR"
            )
        )

        append_validation_event(
            event_type,
            payload,
            log_dir=effective_log_dir,
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
    Extract stable fields from the candidate dictionary.

    run_full_scan stores the quality value as quality_score. Older
    callers may use entry_quality_score. Both are normalised into the
    validation field entry_quality_score.
    """

    signal = candidate.get("signal")

    entry_quality_score = candidate.get(
        "entry_quality_score"
    )

    if entry_quality_score is None:
        entry_quality_score = candidate.get(
            "quality_score"
        )

    entry_quality_detail = candidate.get(
        "entry_quality_detail"
    )

    if (
        entry_quality_detail is None
        and signal is not None
    ):
        entry_quality_detail = getattr(
            signal,
            "entry_quality_detail",
            None,
        )

    return {
        "symbol": candidate.get("symbol"),
        "ranking_score": candidate.get(
            "ranking_score"
        ),
        "entry_quality_score":
            entry_quality_score,
        "entry_quality_detail":
            entry_quality_detail,
        "entry_context_score": candidate.get(
            "entry_context_score"
        ),
        "entry_context_detail": candidate.get(
            "entry_context_detail"
        ),
        "relative_strength_score":
            candidate.get(
                "relative_strength_score"
            ),
        "relative_strength_detail":
            candidate.get(
                "relative_strength_detail"
            ),
        "signal": signal_snapshot(signal),
    }
