from __future__ import annotations

import ast
from pathlib import Path

import main
from cooperative_position_monitor import (
    CooperativeScanMonitor,
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


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


clock = FakeClock()

monitor = CooperativeScanMonitor(
    interval_seconds=25,
    clock_fn=clock,
)

check(
    "Monitor is not due immediately",
    monitor.due() is False,
)

clock.advance(24)

check(
    "Monitor is not due before 25 seconds",
    monitor.due() is False,
)

clock.advance(1)

check(
    "Monitor becomes due at 25 seconds",
    monitor.due() is True,
)

monitor.mark_checked()

check(
    "mark_checked resets monitor",
    monitor.due() is False,
)

check(
    "Completed-check count increments",
    monitor.completed_checks == 1,
)

clock.advance(25)

calls = []

open_positions = {
    "FIRST": {
        "direction": "BUY",
        "entry": 100.0,
        "stop": 99.0,
        "target": 101.0,
        "qty": 1,
        "exchange": "NSE",
    },
    "SECOND": {
        "direction": "BUY",
        "entry": 200.0,
        "stop": 198.0,
        "target": 202.0,
        "qty": 1,
        "exchange": "NSE",
    },
}

original_check = main.check_position_exit


def fake_check_position_exit(
    kite,
    symbol,
    tokens,
    exchange_map,
    positions,
    risk,
    check_trend=False,
):
    calls.append(
        {
            "symbol": symbol,
            "check_trend": check_trend,
        }
    )

    if symbol == "FIRST":
        del positions[symbol]

    return "test check completed"


main.check_position_exit = fake_check_position_exit

try:
    checked = main._cooperative_position_check_if_due(
        kite=object(),
        tokens={
            "FIRST": 1,
            "SECOND": 2,
        },
        exchange_map={
            "FIRST": "NSE",
            "SECOND": "NSE",
        },
        open_positions=open_positions,
        risk=object(),
        monitor=monitor,
    )
finally:
    main.check_position_exit = original_check

check(
    "Every snapshot position was checked",
    checked == ["FIRST", "SECOND"],
)

check(
    "Position deletion during iteration was safe",
    (
        "FIRST" not in open_positions
        and "SECOND" in open_positions
    ),
)

check(
    "Cooperative checks disable trend exits",
    all(
        call["check_trend"] is False
        for call in calls
    ),
)

check(
    "Second cooperative batch was recorded",
    monitor.completed_checks == 2,
)

source = Path("main.py").read_text(
    encoding="utf-8"
)

tree = ast.parse(source)

helpers = [
    node
    for node in tree.body
    if (
        isinstance(node, ast.FunctionDef)
        and node.name
        == "_cooperative_position_check_if_due"
    )
]

scan = next(
    node
    for node in tree.body
    if (
        isinstance(node, ast.FunctionDef)
        and node.name == "run_full_scan"
    )
)

hooks = [
    node
    for node in ast.walk(scan)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        == "_cooperative_position_check_if_due"
    )
]

check(
    "Exactly one cooperative helper exists",
    len(helpers) == 1,
)

check(
    "At least three scan hooks exist",
    len(hooks) >= 3,
)

check(
    "Same-scan re-entry guard exists",
    "protected_position_symbols" in source,
)

combined_source = (
    source
    + Path(
        "cooperative_position_monitor.py"
    ).read_text(
        encoding="utf-8"
    )
)

for forbidden in (
    "import threading",
    "from threading",
    "ThreadPoolExecutor",
    "ProcessPoolExecutor",
    "multiprocessing",
    "asyncio.create_task",
):
    check(
        f"No unsafe concurrency: {forbidden}",
        forbidden not in combined_source,
    )

print()
print(
    f"FINAL RESULTS: {passed} passed, "
    f"{failed} failed"
)

if failed:
    raise SystemExit(1)

print()
print(
    "COOPERATIVE POSITION-MONITOR TESTS PASSED"
)
