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

tree = ast.parse(source)

run_full_scan = next(
    node
    for node in tree.body
    if (
        isinstance(node, ast.FunctionDef)
        and node.name == "run_full_scan"
    )
)

event_types = []
reason_codes = []
rejection_lines = []

for node in ast.walk(run_full_scan):
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        == "record_validation_event"
    ):
        continue

    if not (
        node.args
        and isinstance(
            node.args[0],
            ast.Constant,
        )
    ):
        continue

    event_type = node.args[0].value
    event_types.append(event_type)

    if event_type != "candidate_rejected":
        continue

    rejection_lines.append(node.lineno)

    if (
        len(node.args) >= 2
        and isinstance(node.args[1], ast.Dict)
    ):
        for key, value in zip(
            node.args[1].keys,
            node.args[1].values,
        ):
            if (
                isinstance(key, ast.Constant)
                and key.value == "reason_code"
                and isinstance(value, ast.Constant)
            ):
                reason_codes.append(value.value)


expected_codes = {
    "MARKET_ALIGNMENT_FILTER",
    "ENTRY_OVEREXTENDED",
    "OPPOSING_CHOCH",
    "NEWS_FILTER",
}

check(
    "Candidate collection remains connected once",
    event_types.count("candidate_collected") == 1,
)

check(
    "Exactly four candidate rejections exist",
    event_types.count("candidate_rejected") == 4,
)

check(
    "All four rejection codes are present",
    set(reason_codes) == expected_codes,
)

ranking_lines = [
    node.lineno
    for node in ast.walk(run_full_scan)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        == "rank_entry_candidates"
    )
]

check(
    "Candidate ranking call remains once",
    len(ranking_lines) == 1,
)

check(
    "All rejection events occur before ranking",
    (
        len(ranking_lines) == 1
        and all(
            line < ranking_lines[0]
            for line in rejection_lines
        )
    ),
)

check(
    "Entry order call remains once",
    sum(
        1
        for node in ast.walk(run_full_scan)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            == "place_entry_order"
        )
    )
    == 1,
)

check(
    "Four rejection signal snapshots exist",
    sum(
        1
        for node in ast.walk(run_full_scan)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            == "signal_snapshot"
        )
    )
    == 4,
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
    "PRE-RANKING REJECTION VALIDATION "
    "TESTS PASSED"
)
