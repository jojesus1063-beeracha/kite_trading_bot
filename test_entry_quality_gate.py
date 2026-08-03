from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from entry_quality import (
    MAX_VWAP_DISTANCE_ATR,
    assess_entry_quality,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


def controlled_candles():
    periods = 40

    dates = pd.date_range(
        "2026-08-03 09:15:00+05:30",
        periods=periods,
        freq="5min",
    )

    closes = [
        100.02 if index % 2 == 0
        else 99.98
        for index in range(periods)
    ]

    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [
                value - 0.01
                for value in closes
            ],
            "high": [
                value + 0.12
                for value in closes
            ],
            "low": [
                value - 0.12
                for value in closes
            ],
            "close": closes,
            "volume": [
                1000 + index * 5
                for index in range(periods)
            ],
        }
    )

    frame["ema_entry"] = (
        frame["close"]
        .ewm(
            span=9,
            adjust=False,
        )
        .mean()
    )

    return frame


signal = SimpleNamespace(
    direction="BUY",
)

normal = controlled_candles()

normal_result = assess_entry_quality(
    signal,
    normal,
)

print(
    "Controlled diagnostic:",
    normal_result.detail,
)

check(
    "Controlled candle is accepted",
    normal_result.accepted,
)

check(
    "Controlled candle receives numeric score",
    isinstance(normal_result.score, float),
)

check(
    "VWAP limit is 2.5 ATR",
    MAX_VWAP_DISTANCE_ATR == 2.50,
)

extended = controlled_candles()
last = extended.index[-1]

extended.loc[last, "open"] = 100.0
extended.loc[last, "low"] = 99.9
extended.loc[last, "close"] = 104.0
extended.loc[last, "high"] = 104.2
extended.loc[last, "ema_entry"] = 100.5

extended_result = assess_entry_quality(
    signal,
    extended,
)

print(
    "Extended diagnostic:",
    extended_result.detail,
)

check(
    "Large extended candle is rejected",
    not extended_result.accepted,
)

check(
    "Rejection reason is explicit",
    (
        "overextended"
        in extended_result.reason
        or "too far"
        in extended_result.reason
    ),
)

insufficient_result = assess_entry_quality(
    signal,
    normal.head(3),
)

check(
    "Insufficient synthetic data fails open",
    insufficient_result.accepted,
)

print()
print("Entry-quality gate tests passed.")
