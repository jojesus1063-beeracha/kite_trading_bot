"""Lightweight, diagnostics-only scan filter attribution.

This module records the deepest stage reached (or first blocking reason)
for each symbol in a configured entry-candle scan bucket. It deliberately does not
influence trading decisions: callers may ignore every return value.

A JSON snapshot is written to runtime/filter_diagnostics/latest.json so
paper-trading sessions can be inspected without depending on stdout.
When a new entry bucket is observed, the previous bucket's summary is
logged once.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

import config as cfg
from scheduler import candle_interval_minutes

logger = logging.getLogger("filter_diagnostics")

_PROJECT_DIR = Path(__file__).resolve().parent
_OUTPUT_DIR = _PROJECT_DIR / "runtime" / "filter_diagnostics"
_OUTPUT_PATH = _OUTPUT_DIR / "latest.json"
_LOCK = Lock()

_current_scan_key: str | None = None
_symbol_status: dict[str, str] = {}
_symbol_detail: dict[str, dict[str, Any]] = {}


def _normalise_scan_key(value: Any = None) -> str:
    """Return a stable configured entry bucket key."""
    interval = candle_interval_minutes(cfg.ENTRY_TIMEFRAME)
    try:
        if value is not None:
            if hasattr(value, "to_pydatetime"):
                value = value.to_pydatetime()
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime):
                minute = value.minute - (value.minute % interval)
                return value.replace(minute=minute, second=0, microsecond=0).isoformat()
    except Exception:
        pass

    now = datetime.now()
    minute = now.minute - (now.minute % interval)
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()


def _summary() -> dict[str, int]:
    return dict(sorted(Counter(_symbol_status.values()).items()))


def _write_snapshot() -> None:
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "scan_key": _current_scan_key,
            "updated_at": datetime.now().isoformat(),
            "symbol_count": len(_symbol_status),
            "summary": _summary(),
            "symbols": {
                symbol: {
                    "status": _symbol_status[symbol],
                    "detail": _symbol_detail.get(symbol, {}),
                }
                for symbol in sorted(_symbol_status)
            },
        }
        tmp = _OUTPUT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _OUTPUT_PATH)
    except Exception:
        logger.exception("filter diagnostics snapshot write failed")


def _log_previous_summary() -> None:
    if not _current_scan_key or not _symbol_status:
        return
    logger.info(
        "SCAN FILTER SUMMARY | scan=%s | symbols=%s | %s",
        _current_scan_key,
        len(_symbol_status),
        " | ".join(f"{k}={v}" for k, v in _summary().items()),
    )


def mark_filter_status(
    symbol: str,
    status: str,
    *,
    scan_time: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record diagnostics for a symbol; never raises and never blocks trades."""
    global _current_scan_key, _symbol_status, _symbol_detail

    try:
        scan_key = _normalise_scan_key(scan_time)
        with _LOCK:
            if _current_scan_key is None:
                _current_scan_key = scan_key
            elif scan_key != _current_scan_key:
                _log_previous_summary()
                _current_scan_key = scan_key
                _symbol_status = {}
                _symbol_detail = {}

            _symbol_status[str(symbol)] = str(status)
            if detail:
                _symbol_detail[str(symbol)] = dict(detail)
            _write_snapshot()
    except Exception:
        logger.exception("filter diagnostics mark failed for %s", symbol)


def get_filter_summary() -> dict[str, int]:
    """Return a copy of the current in-memory summary for tests/tools."""
    with _LOCK:
        return _summary().copy()


def reset_filter_diagnostics() -> None:
    """Test/helper reset. Does not remove persisted history outside latest.json."""
    global _current_scan_key, _symbol_status, _symbol_detail
    with _LOCK:
        _current_scan_key = None
        _symbol_status = {}
        _symbol_detail = {}
