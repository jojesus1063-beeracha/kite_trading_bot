from __future__ import annotations

from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from validation_log import load_validation_events
from validation_recorder import (
    candidate_snapshot,
    record_validation_event,
    signal_snapshot,
)


passed = 0
failed = 0


def check(name, condition):
    global passed, failed

    if condition:
        passed += 1
        print("PASS:", name)
    else:
        failed += 1
        print("FAIL:", name)


signal = SimpleNamespace(
    symbol="TEST",
    direction="BUY",
    entry_price=100.0,
    stop_loss=99.55,
    target=100.70,
    timestamp="2026-08-03T10:15:00+05:30",
    reason="test signal",
    confidence="HIGH",
    market_alignment="ALIGNED",
    price_action_score=15,
    entry_quality_score=92.5,
    entry_quality_detail={
        "atr": 2.0,
    },
    entry_context_score=8.0,
    entry_context_detail={
        "adx_state": "RISING",
    },
    relative_strength_score=6.5,
    relative_strength_detail={
        "market_edge_pct": 0.4,
    },
)

snapshot = signal_snapshot(signal)

check(
    "Signal symbol captured",
    snapshot["symbol"] == "TEST",
)

check(
    "Signal direction captured",
    snapshot["direction"] == "BUY",
)

check(
    "Signal quality detail captured",
    snapshot["entry_quality_detail"]["atr"]
    == 2.0,
)

candidate = {
    "symbol": "TEST",
    "signal": signal,
    "ranking_score": 171.5,
    "entry_quality_score": 92.5,
    "entry_quality_detail": {
        "atr": 2.0,
    },
    "entry_context_score": 8.0,
    "entry_context_detail": {
        "adx_state": "RISING",
    },
    "relative_strength_score": 6.5,
    "relative_strength_detail": {
        "market_edge_pct": 0.4,
    },
}

candidate_data = candidate_snapshot(
    candidate
)

check(
    "Candidate ranking captured",
    candidate_data["ranking_score"]
    == 171.5,
)

check(
    "Candidate embeds signal snapshot",
    candidate_data["signal"]["symbol"]
    == "TEST",
)

with tempfile.TemporaryDirectory() as temp:
    written = record_validation_event(
        "candidate_collected",
        candidate_data,
        log_dir=temp,
    )

    check(
        "Successful ledger write returns True",
        written is True,
    )

    files = list(
        __import__("pathlib")
        .Path(temp)
        .glob("*.jsonl")
    )

    check(
        "One daily ledger file created",
        len(files) == 1,
    )

    events = load_validation_events(
        files[0].stem,
        log_dir=temp,
    )

    check(
        "Candidate event loads successfully",
        len(events) == 1,
    )

    check(
        "Loaded event type is correct",
        events[0]["event_type"]
        == "candidate_collected",
    )

with patch(
    "validation_recorder.append_validation_event",
    side_effect=OSError(
        "simulated disk failure"
    ),
):
    failed_write = record_validation_event(
        "candidate_rejected",
        {
            "symbol": "TEST",
        },
    )

check(
    "Ledger failure returns False",
    failed_write is False,
)

check(
    "Ledger failure does not raise",
    True,
)

empty_snapshot = signal_snapshot(None)

check(
    "None signal produces empty snapshot",
    empty_snapshot == {},
)

print()
print(
    f"FINAL RESULTS: "
    f"{passed} passed, "
    f"{failed} failed"
)

if failed:
    raise SystemExit(1)

print()
print(
    "VALIDATION RECORDER TESTS PASSED"
)
