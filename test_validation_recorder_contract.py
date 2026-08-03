from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile

from validation_recorder import (
    candidate_snapshot,
    record_validation_event,
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
    entry_quality_detail={
        "body_atr_ratio": 0.50,
    },
)

candidate = {
    "symbol": "TEST",
    "signal": signal,
    "ranking_score": 151.5,
    "quality_score": 92.0,
    "entry_context_score": 8.0,
    "entry_context_detail": {
        "adx_state": "RISING",
    },
    "relative_strength_score": 4.0,
    "relative_strength_detail": {
        "market_edge_pct": 0.30,
    },
}

snapshot = candidate_snapshot(candidate)

check(
    "Actual quality_score is normalised",
    snapshot["entry_quality_score"]
    == 92.0,
)

check(
    "Signal quality detail is retained",
    snapshot["entry_quality_detail"][
        "body_atr_ratio"
    ]
    == 0.50,
)

check(
    "Ranking score is retained",
    snapshot["ranking_score"]
    == 151.5,
)

production_dir = Path(
    "validation_events"
)

production_before = (
    sorted(production_dir.glob("*"))
    if production_dir.exists()
    else None
)

with tempfile.TemporaryDirectory() as temp:
    previous = os.environ.get(
        "KITE_VALIDATION_LOG_DIR"
    )

    os.environ[
        "KITE_VALIDATION_LOG_DIR"
    ] = temp

    try:
        result = record_validation_event(
            "contract_test",
            snapshot,
        )
    finally:
        if previous is None:
            os.environ.pop(
                "KITE_VALIDATION_LOG_DIR",
                None,
            )
        else:
            os.environ[
                "KITE_VALIDATION_LOG_DIR"
            ] = previous

    check(
        "Environment-directed write succeeds",
        result is True,
    )

    files = list(
        Path(temp).glob("*.jsonl")
    )

    check(
        "One isolated ledger file created",
        len(files) == 1,
    )

production_after = (
    sorted(production_dir.glob("*"))
    if production_dir.exists()
    else None
)

check(
    "Production ledger remains untouched",
    production_before
    == production_after,
)

check(
    "Runtime ledger directory is ignored",
    "validation_events/"
    in Path(".gitignore").read_text(
        encoding="utf-8"
    ).splitlines(),
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
    "VALIDATION RECORDER CONTRACT "
    "TESTS PASSED"
)
