from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from entry_confirmation import (
    assess_entry_context,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


dates = pd.date_range(
    "2026-08-03 09:15:00+05:30",
    periods=4,
    freq="15min",
)

rising_adx = pd.DataFrame(
    {
        "date": dates,
        "adx": [
            22.0,
            24.0,
            26.0,
            29.0,
        ],
    }
)

falling_adx = pd.DataFrame(
    {
        "date": dates,
        "adx": [
            30.0,
            29.0,
            27.0,
            24.0,
        ],
    }
)


def signal_with(detail):
    return SimpleNamespace(
        timestamp=dates[-1],
        direction="BUY",
        price_action_detail=detail,
    )


pullback = assess_entry_context(
    signal_with(
        {
            "market_structure": False,
            "breakout": False,
            "pullback": True,
            "bos": False,
            "choch": False,
        }
    ),
    rising_adx,
)

check(
    "Confirmed pullback is accepted",
    pullback.accepted,
)

check(
    "Confirmed pullback receives positive score",
    pullback.score_adjustment > 0,
)

breakout = assess_entry_context(
    signal_with(
        {
            "market_structure": False,
            "breakout": True,
            "pullback": False,
            "bos": False,
            "choch": False,
        }
    ),
    rising_adx,
)

check(
    "Confirmed breakout is accepted",
    breakout.accepted,
)

unconfirmed = assess_entry_context(
    signal_with(
        {
            "market_structure": False,
            "breakout": False,
            "pullback": False,
            "bos": False,
            "choch": False,
        }
    ),
    rising_adx,
)

check(
    "Unconfirmed candidate remains eligible",
    unconfirmed.accepted,
)

check(
    "Unconfirmed candidate receives ranking penalty",
    unconfirmed.score_adjustment
    < pullback.score_adjustment,
)

choch = assess_entry_context(
    signal_with(
        {
            "market_structure": True,
            "breakout": True,
            "pullback": False,
            "bos": True,
            "choch": True,
        }
    ),
    rising_adx,
)

check(
    "Opposing CHoCH blocks candidate",
    not choch.accepted,
)

rising = assess_entry_context(
    signal_with(
        {
            "market_structure": True,
            "breakout": False,
            "pullback": False,
            "bos": False,
            "choch": False,
        }
    ),
    rising_adx,
)

falling = assess_entry_context(
    signal_with(
        {
            "market_structure": True,
            "breakout": False,
            "pullback": False,
            "bos": False,
            "choch": False,
        }
    ),
    falling_adx,
)

check(
    "Rising ADX ranks above falling ADX",
    (
        rising.score_adjustment
        > falling.score_adjustment
    ),
)

source = Path("main.py").read_text(
    encoding="utf-8"
)

tree = ast.parse(source)

scan = next(
    node
    for node in tree.body
    if (
        isinstance(node, ast.FunctionDef)
        and node.name == "run_full_scan"
    )
)

locations = {
    "quality": [],
    "context": [],
    "ranking": [],
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
            "assess_entry_context": "context",
            "rank_entry_candidates": "ranking",
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
    "Context assessment occurs before ranking",
    (
        min(locations["quality"])
        < min(locations["context"])
        < min(locations["ranking"])
    ),
)

check(
    "Execution safeguards remain ordered",
    (
        min(locations["ranking"])
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
    "Ranking includes entry-context score",
    "entry_context.score_adjustment"
    in scan_source,
)

print()
print("Entry-context ranking tests passed.")
