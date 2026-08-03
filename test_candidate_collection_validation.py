from __future__ import annotations

import ast
from pathlib import Path


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


source = Path("main.py").read_text(
    encoding="utf-8"
)

check(
    "Validation recorder is imported",
    (
        "from validation_recorder import ("
        in source
    ),
)

tree = ast.parse(source)

run_full_scan = next(
    node
    for node in tree.body
    if (
        isinstance(node, ast.FunctionDef)
        and node.name == "run_full_scan"
    )
)

event_calls = []

for node in ast.walk(run_full_scan):
    if not isinstance(node, ast.Call):
        continue

    if not (
        isinstance(node.func, ast.Name)
        and node.func.id
        == "record_validation_event"
    ):
        continue

    event_calls.append(node)

check(
    "Exactly one validation event is currently connected",
    len(event_calls) == 1,
)

event_type = None

if event_calls:
    call = event_calls[0]

    if (
        call.args
        and isinstance(
            call.args[0],
            ast.Constant,
        )
    ):
        event_type = call.args[0].value

check(
    "Connected event is candidate_collected",
    event_type == "candidate_collected",
)

candidate_snapshot_calls = [
    node
    for node in ast.walk(run_full_scan)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "candidate_snapshot"
    )
]

check(
    "Candidate snapshot is used once",
    len(candidate_snapshot_calls) == 1,
)

check(
    "Entry-order call count remains one",
    sum(
        1
        for node in ast.walk(run_full_scan)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "place_entry_order"
        )
    )
    == 1,
)

check(
    "Candidate ranking call count remains one",
    sum(
        1
        for node in ast.walk(run_full_scan)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            == "rank_entry_candidates"
        )
    )
    == 1,
)

append_position = source.find(
    "entry_candidates.append("
)

event_position = source.find(
    '"candidate_collected"'
)

ranking_position = source.find(
    "ranked_candidates = "
    "rank_entry_candidates("
)

check(
    "Event occurs after candidate collection",
    (
        append_position >= 0
        and event_position > append_position
    ),
)

check(
    "Event occurs before ranking",
    (
        ranking_position >= 0
        and event_position < ranking_position
    ),
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
    "CANDIDATE COLLECTION VALIDATION "
    "TESTS PASSED"
)
