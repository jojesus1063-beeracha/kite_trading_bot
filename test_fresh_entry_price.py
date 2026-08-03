from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from entry_quality import (
    MAX_ABSOLUTE_SIGNAL_DRIFT_PCT,
    MAX_ADVERSE_LIVE_SLIPPAGE_PCT,
    fetch_live_price,
    validate_live_price,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


class FakeKite:
    def __init__(self, price):
        self.price = price
        self.calls = []

    def quote(self, instruments):
        self.calls.append(
            list(instruments)
        )

        return {
            "NSE:TEST": {
                "last_price": self.price,
            }
        }


kite = FakeKite(100.10)

check(
    "Numeric live quote is returned",
    fetch_live_price(
        kite,
        "NSE",
        "TEST",
    )
    == 100.10,
)

check(
    "One quote request is made",
    kite.calls == [["NSE:TEST"]],
)

buy = SimpleNamespace(
    direction="BUY",
    entry_price=100.0,
)

sell = SimpleNamespace(
    direction="SELL",
    entry_price=100.0,
)

check(
    "BUY +0.10% is accepted",
    validate_live_price(
        buy,
        100.10,
    ).accepted,
)

check(
    "BUY +0.20% is rejected",
    not validate_live_price(
        buy,
        100.20,
    ).accepted,
)

check(
    "BUY better price is accepted",
    validate_live_price(
        buy,
        99.80,
    ).accepted,
)

check(
    "SELL -0.10% is accepted",
    validate_live_price(
        sell,
        99.90,
    ).accepted,
)

check(
    "SELL -0.20% is rejected",
    not validate_live_price(
        sell,
        99.80,
    ).accepted,
)

check(
    "SELL better price is accepted",
    validate_live_price(
        sell,
        100.20,
    ).accepted,
)

check(
    "Absolute drift over 0.35% is rejected",
    not validate_live_price(
        buy,
        99.60,
    ).accepted,
)

check(
    "Unavailable quote preserves existing path",
    validate_live_price(
        buy,
        None,
    ).accepted,
)

check(
    "Adverse limit remains 0.15%",
    MAX_ADVERSE_LIVE_SLIPPAGE_PCT
    == 0.15,
)

check(
    "Absolute drift limit remains 0.35%",
    MAX_ABSOLUTE_SIGNAL_DRIFT_PCT
    == 0.35,
)

source = Path("main.py").read_text(
    encoding="utf-8"
)

tree = ast.parse(source)

scan = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "run_full_scan"
)

locations = {
    "quality": [],
    "fresh": [],
    "sizing": [],
    "order": [],
}

for node in ast.walk(scan):
    if not isinstance(node, ast.Call):
        continue

    if isinstance(node.func, ast.Name):
        if node.func.id == "assess_entry_quality":
            locations["quality"].append(
                node.lineno
            )

        if node.func.id == "validate_live_price":
            locations["fresh"].append(
                node.lineno
            )

        if node.func.id == "place_entry_order":
            locations["order"].append(
                node.lineno
            )

    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "position_size"
    ):
        locations["sizing"].append(
            node.lineno
        )

for key, values in locations.items():
    check(
        f"{key} pipeline stage exists",
        bool(values),
    )

check(
    "Entry pipeline order is correct",
    (
        min(locations["quality"])
        < min(locations["fresh"])
        < min(locations["sizing"])
        < min(locations["order"])
    ),
)

print()
print("Fresh-entry-price tests passed.")
