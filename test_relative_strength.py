from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from relative_strength import (
    MAX_POINTS_PER_BENCHMARK,
    RELATIVE_STRENGTH_LOOKBACK_BARS,
    assess_relative_strength,
    completed_return_pct,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


dates = pd.date_range(
    "2026-08-03 09:15:00+05:30",
    periods=6,
    freq="15min",
)


def candles(closes):
    return pd.DataFrame(
        {
            "date": dates,
            "close": closes,
        }
    )


buy_signal = SimpleNamespace(
    direction="BUY",
    timestamp=dates[-1],
)

sell_signal = SimpleNamespace(
    direction="SELL",
    timestamp=dates[-1],
)

stock_up = candles(
    [
        100.0,
        100.1,
        100.3,
        100.5,
        100.8,
        101.2,
    ]
)

market_up_slowly = candles(
    [
        100.0,
        100.05,
        100.10,
        100.15,
        100.20,
        100.25,
    ]
)

sector_up_moderately = candles(
    [
        100.0,
        100.05,
        100.10,
        100.20,
        100.35,
        100.50,
    ]
)

buy_result = assess_relative_strength(
    buy_signal,
    stock_up,
    market_up_slowly,
    sector_up_moderately,
)

check(
    "BUY outperformer receives positive score",
    buy_result.score_adjustment > 0,
)

check(
    "BUY market edge is positive",
    buy_result.detail["market_edge_pct"] > 0,
)

check(
    "BUY sector edge is positive",
    buy_result.detail["sector_edge_pct"] > 0,
)

stock_down = candles(
    [
        100.0,
        99.9,
        99.7,
        99.4,
        99.1,
        98.7,
    ]
)

market_down_slowly = candles(
    [
        100.0,
        99.95,
        99.90,
        99.85,
        99.80,
        99.75,
    ]
)

sell_result = assess_relative_strength(
    sell_signal,
    stock_down,
    market_down_slowly,
    None,
)

check(
    "SELL stock falling faster than Nifty "
    "receives positive score",
    sell_result.score_adjustment > 0,
)

buy_laggard = assess_relative_strength(
    buy_signal,
    market_up_slowly,
    stock_up,
    sector_up_moderately,
)

check(
    "BUY laggard receives negative score",
    buy_laggard.score_adjustment < 0,
)

unavailable = assess_relative_strength(
    buy_signal,
    stock_up.iloc[:2],
    pd.DataFrame(),
    pd.DataFrame(),
)

check(
    "Unavailable benchmark data fails open",
    unavailable.score_adjustment == 0,
)

check(
    "Relative-strength lookback remains four bars",
    RELATIVE_STRENGTH_LOOKBACK_BARS == 4,
)

extreme_stock = candles(
    [
        100.0,
        100.0,
        101.0,
        103.0,
        107.0,
        112.0,
    ]
)

extreme_result = assess_relative_strength(
    buy_signal,
    extreme_stock,
    market_up_slowly,
    sector_up_moderately,
)

check(
    "Each benchmark contribution is capped",
    (
        abs(
            extreme_result.detail[
                "market_score"
            ]
        )
        <= MAX_POINTS_PER_BENCHMARK
        and abs(
            extreme_result.detail[
                "sector_score"
            ]
        )
        <= MAX_POINTS_PER_BENCHMARK
    ),
)

return_value = completed_return_pct(
    stock_up,
    dates[-1],
)

check(
    "Completed return calculation is numeric",
    isinstance(return_value, float),
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
    "relative": [],
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
            "assess_relative_strength": "relative",
            "rank_entry_candidates": "ranking",
            "validate_live_price": "fresh",
            "place_entry_order": "order",
        }

        key = mapping.get(
            node.func.id
        )

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
    "Relative strength runs before ranking",
    (
        min(locations["quality"])
        < min(locations["context"])
        < min(locations["relative"])
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
    "Ranking contains relative-strength score",
    "relative_strength.score_adjustment"
    in scan_source,
)

print()
print("Relative-strength tests passed.")
