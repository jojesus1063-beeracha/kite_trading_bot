#!/usr/bin/env python3
"""Diagnostics-only enrichment for breakout validation audit events.

This module MUST NOT change signal selection or order behaviour. It only adds
human/machine-readable detail to an existing audit event when a nested
``breakout_validation`` payload is present.
"""
from __future__ import annotations

from typing import Any, Mapping


_COMPONENTS = (
    ("STRUCTURE_FAIL", "structure_confirmed"),
    ("VOLUME_FAIL", "volume_confirmed"),
    ("ATR_EXPANSION_FAIL", "volatility_confirmed"),
    ("CLV_FAIL", "clv_confirmed"),
)


def _find_breakout_validation(value: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
    """Find the first breakout_validation mapping in a JSON-like event."""
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        direct = value.get("breakout_validation")
        if isinstance(direct, Mapping):
            return direct
        for child in value.values():
            found = _find_breakout_validation(child, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _find_breakout_validation(child, depth=depth + 1)
            if found is not None:
                return found
    return None


def enrich_breakout_diagnostics(event: Any) -> Any:
    """Return *event* enriched with breakout diagnostics, or unchanged.

    The function is deliberately fail-open for telemetry: malformed or absent
    breakout data never changes the trading decision and never raises into the
    signal path.
    """
    if not isinstance(event, dict):
        return event

    try:
        breakout = _find_breakout_validation(event)
        if not isinstance(breakout, Mapping):
            return event

        metrics = breakout.get("metrics")
        if not isinstance(metrics, Mapping):
            metrics = {}

        failed_components = [
            label
            for label, metric_key in _COMPONENTS
            if metrics.get(metric_key) is False
        ]

        passed = breakout.get("passed") is True
        diagnostics = {
            "status": "PASS" if passed else "FAIL",
            "failed_components": failed_components,
            "structure_break": metrics.get("structure_confirmed"),
            "volume_ratio": metrics.get("volume_ratio"),
            "volume_threshold": metrics.get("minimum_volume_ratio"),
            "volume_pass": metrics.get("volume_confirmed"),
            "atr_multiple": metrics.get("atr_multiplier"),
            "atr_threshold": metrics.get("minimum_atr_multiplier"),
            "atr_pass": metrics.get("volatility_confirmed"),
            "clv_value": metrics.get("clv"),
            "clv_threshold": metrics.get("clv_threshold"),
            "clv_pass": metrics.get("clv_confirmed"),
        }

        if passed:
            diagnostics["primary_rejection_reason"] = None
            diagnostics["secondary_rejection_reasons"] = []
        elif len(failed_components) == 1:
            diagnostics["primary_rejection_reason"] = failed_components[0]
            diagnostics["secondary_rejection_reasons"] = []
        elif failed_components:
            diagnostics["primary_rejection_reason"] = "MULTIPLE_BREAKOUT_COMPONENTS_FAILED"
            diagnostics["secondary_rejection_reasons"] = failed_components
        else:
            diagnostics["primary_rejection_reason"] = "BREAKOUT_VALIDATION_FAILED_UNCLASSIFIED"
            diagnostics["secondary_rejection_reasons"] = []

        event["breakout_diagnostics"] = diagnostics
        return event
    except Exception as exc:  # telemetry must never break trading
        event.setdefault("breakout_diagnostics", {
            "status": "DIAGNOSTIC_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return event
