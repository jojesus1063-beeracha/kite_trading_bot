from __future__ import annotations

import ast
from pathlib import Path

from entry_quality import (
    fetch_live_prices,
    rank_entry_candidates,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


candidates = [
    {
        "symbol": "ALPHA",
        "exchange": "NSE",
        "ranking_score": 120.0,
        "quality_score": 80.0,
    },
    {
        "symbol": "BETA",
        "exchange": "NSE",
        "ranking_score": 140.0,
        "quality_score": 70.0,
    },
    {
        "symbol": "GAMMA",
        "exchange": "NSE",
        "ranking_score": 140.0,
        "quality_score": 90.0,
    },
]

ranked = rank_entry_candidates(candidates)

check(
    "Highest score ranks first",
    [
        item["symbol"]
        for item in ranked
    ]
    == [
        "GAMMA",
        "BETA",
        "ALPHA",
    ],
)


class FakeBatchKite:
    def __init__(self):
        self.calls = []

    def quote(self, instruments):
        self.calls.append(
            list(instruments)
        )

        return {
            "NSE:GAMMA": {
                "last_price": 103.0,
            },
            "NSE:BETA": {
                "last_price": 102.0,
            },
            "NSE:ALPHA": {
                "last_price": 101.0,
            },
        }


kite = FakeBatchKite()

prices = fetch_live_prices(
    kite,
    ranked,
)

check(
    "One batch quote request is used",
    len(kite.calls) == 1,
)

check(
    "Batch contains every candidate",
    set(kite.calls[0])
    == {
        "NSE:GAMMA",
        "NSE:BETA",
        "NSE:ALPHA",
    },
)

check(
    "Prices are mapped by symbol",
    prices
    == {
        "GAMMA": 103.0,
        "BETA": 102.0,
        "ALPHA": 101.0,
    },
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

primary_loop = next(
    node
    for node in scan.body
    if isinstance(node, ast.For)
    and isinstance(node.target, ast.Name)
    and node.target.id == "symbol"
)

orders_in_primary_loop = [
    node
    for node in ast.walk(primary_loop)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "place_entry_order"
    )
]

check(
    "Primary scan loop places no order",
    not orders_in_primary_loop,
)

candidate_appends = [
    node
    for node in ast.walk(primary_loop)
    if (
        isinstance(node, ast.Call)
        and isinstance(
            node.func,
            ast.Attribute,
        )
        and node.func.attr == "append"
        and isinstance(
            node.func.value,
            ast.Name,
        )
        and node.func.value.id
        == "entry_candidates"
    )
]

check(
    "Primary scan loop collects candidates",
    bool(candidate_appends),
)

locations = {
    "quality": [],
    "ranking": [],
    "batch": [],
    "fresh": [],
    "sizing": [],
    "order": [],
}

for node in ast.walk(scan):
    if not isinstance(node, ast.Call):
        continue

    if isinstance(node.func, ast.Name):
        mapping = {
            "assess_entry_quality": "quality",
            "rank_entry_candidates": "ranking",
            "fetch_live_prices": "batch",
            "validate_live_price": "fresh",
            "place_entry_order": "order",
        }

        key = mapping.get(node.func.id)

        if key:
            locations[key].append(
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
    "Ranking starts after full scan",
    min(locations["ranking"])
    > primary_loop.end_lineno,
)

check(
    "Order placement occurs after full scan",
    min(locations["order"])
    > primary_loop.end_lineno,
)

check(
    "Ranked pipeline order is correct",
    (
        min(locations["quality"])
        < min(locations["ranking"])
        < min(locations["batch"])
        < min(locations["fresh"])
        < min(locations["sizing"])
        < min(locations["order"])
    ),
)

scan_source = ast.get_source_segment(
    source,
    scan,
) or ""

check(
    "Confirmed fill quantity is preserved",
    (
        'confirmed_qty = result["filled_quantity"]'
        in scan_source
        or
        "confirmed_qty = result['filled_quantity']"
        in scan_source
    ),
)

check(
    "Position stores confirmed quantity",
    '"qty": confirmed_qty'
    in scan_source,
)

check(
    "Fixed levels still use confirmed fill",
    (
        "fixed_levels_from_fill("
        in scan_source
        and "confirmed_entry_price"
        in scan_source
    ),
)

print()
print("Ranked-candidate scan tests passed.")
