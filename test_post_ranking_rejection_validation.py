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

reason_lines = {}

for node in ast.walk(run_full_scan):
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        == "record_validation_event"
    ):
        continue

    if (
        len(node.args) < 2
        or not isinstance(node.args[1], ast.Dict)
    ):
        continue

    for key, value in zip(
        node.args[1].keys,
        node.args[1].values,
    ):
        if (
            isinstance(key, ast.Constant)
            and key.value == "reason_code"
            and isinstance(value, ast.Constant)
        ):
            reason_lines[value.value] = node.lineno


expected = {
    "SAME_SCAN_POSITION_EXIT",
    "OUTSIDE_TRADING_WINDOW",
    "RISK_LIMIT_REACHED",
    "FRESH_PRICE_REJECTED",
}

check(
    "All four post-ranking rejection codes exist",
    expected.issubset(
        set(reason_lines)
    ),
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

order_lines = [
    node.lineno
    for node in ast.walk(run_full_scan)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        == "place_entry_order"
    )
]

check(
    "Candidate ranking call remains once",
    len(ranking_lines) == 1,
)

check(
    "Entry-order call remains once",
    len(order_lines) == 1,
)

check(
    "All four events occur after ranking",
    (
        len(ranking_lines) == 1
        and all(
            reason_lines[code]
            > ranking_lines[0]
            for code in expected
        )
    ),
)

check(
    "All four rejection events occur before order submission",
    (
        len(order_lines) == 1
        and all(
            reason_lines[code]
            < order_lines[0]
            for code in expected
        )
    ),
)

check(
    "Candidate context is constructed once",
    source.count(
        "candidate_event_context = {"
    )
    == 1,
)

check(
    "Candidate snapshot call count remains one",
    sum(
        1
        for node in ast.walk(run_full_scan)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            == "candidate_snapshot"
        )
    )
    == 1,
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
    "POST-RANKING REJECTION VALIDATION "
    "TESTS PASSED"
)
