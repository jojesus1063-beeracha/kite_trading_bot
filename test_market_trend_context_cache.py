from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import data_feed
from market_trend import (
    clear_relative_strength_cache,
    get_cached_market_candles,
    get_cached_sector_candles,
    get_market_trend,
    get_sector_trend,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


dates = pd.date_range(
    "2026-08-01 09:15:00+05:30",
    periods=70,
    freq="15min",
)

fixture = pd.DataFrame(
    {
        "date": dates,
        "open": [
            100.0 + index * 0.1
            for index in range(70)
        ],
        "high": [
            100.3 + index * 0.1
            for index in range(70)
        ],
        "low": [
            99.7 + index * 0.1
            for index in range(70)
        ],
        "close": [
            100.1 + index * 0.1
            for index in range(70)
        ],
        "volume": [
            1000 + index
            for index in range(70)
        ],
    }
)

cfg = SimpleNamespace(
    TREND_TIMEFRAME="15minute",
    TREND_EMA_FAST=20,
    TREND_EMA_SLOW=50,
    ENTRY_EMA=9,
    VOLUME_LOOKBACK=20,
    ADX_PERIOD=14,
    ADX_MODE="off",
    USE_ADX_FILTER=False,
    ADX_THRESHOLD=25,
)

original_fetch = data_feed.fetch_candles
calls = []


def fake_fetch(
    kite,
    token,
    interval,
    lookback_days=5,
    **kwargs,
):
    calls.append(token)
    return fixture.copy()


data_feed.fetch_candles = fake_fetch

try:
    clear_relative_strength_cache()

    get_market_trend(
        object(),
        cfg,
    )

    market_cached = (
        get_cached_market_candles()
    )

    check(
        "Nifty fetch populates market cache",
        len(market_cached) == len(fixture),
    )

    get_sector_trend(
        object(),
        "HDFCBANK",
        cfg,
    )

    sector_cached = (
        get_cached_sector_candles(
            "NIFTY BANK"
        )
    )

    check(
        "Sector fetch populates sector cache",
        len(sector_cached) == len(fixture),
    )

    check(
        "One market and one sector request occurred",
        len(calls) == 2,
    )

    clear_relative_strength_cache()

    check(
        "Cache clear removes market candles",
        get_cached_market_candles().empty,
    )

    check(
        "Cache clear removes sector candles",
        get_cached_sector_candles(
            "NIFTY BANK"
        ).empty,
    )
finally:
    data_feed.fetch_candles = original_fetch
    clear_relative_strength_cache()

print()
print("Benchmark candle-cache tests passed.")
