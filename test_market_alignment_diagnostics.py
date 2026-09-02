from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

import market_trend


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


cfg = SimpleNamespace(TREND_TIMEFRAME="15minute")
kite = MagicMock()
candles = pd.DataFrame(
    {
        "date": pd.date_range(
            "2026-08-04 09:15",
            periods=3,
            freq="15min",
        ),
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.5, 101.5, 102.5],
        "volume": [1000, 1100, 1200],
    }
)


with patch(
    "data_feed.fetch_candles",
    side_effect=RuntimeError("API unavailable"),
):
    trend, reason = market_trend.get_market_trend_diagnostic(
        kite,
        cfg,
    )

check(
    "Market fetch exceptions are visible without changing the fallback",
    trend == "Sideways" and reason == "FETCH_ERROR",
)


with patch(
    "data_feed.fetch_candles",
    return_value=pd.DataFrame(),
):
    trend, reason = market_trend.get_market_trend_diagnostic(
        kite,
        cfg,
    )

check(
    "Empty market candles have an explicit reason",
    trend == "Sideways" and reason == "EMPTY_DATA",
)


with patch(
    "data_feed.fetch_candles",
    return_value=candles,
), patch.object(
    market_trend,
    "add_indicators",
    side_effect=ValueError("indicator failure"),
):
    trend, reason = market_trend.get_market_trend_diagnostic(
        kite,
        cfg,
    )

check(
    "Indicator failures have an explicit reason",
    trend == "Sideways" and reason == "INDICATOR_ERROR",
)


with patch(
    "data_feed.fetch_candles",
    return_value=candles,
), patch.object(
    market_trend,
    "add_indicators",
    return_value=(candles, candles),
), patch.object(
    market_trend,
    "classify_trend",
    return_value="Bullish",
):
    trend, reason = market_trend.get_market_trend_diagnostic(
        kite,
        cfg,
    )

check(
    "Successful market classification reports OK",
    trend == "Bullish" and reason == "OK",
)


with patch(
    "data_feed.fetch_candles",
    side_effect=AssertionError("unmapped symbols must not fetch"),
):
    trend, reason = market_trend.get_sector_trend_diagnostic(
        kite,
        "UNMAPPED_TEST_SYMBOL",
        cfg,
    )

check(
    "Unmapped sectors remain Sideways through the public data API",
    trend == "Sideways" and reason == "UNMAPPED",
)


tokenless_symbol = "TOKENLESS_TEST_SYMBOL"
tokenless_sector = "NIFTY TOKENLESS TEST"
market_trend.SECTOR_MAP[tokenless_symbol] = tokenless_sector
market_trend.SECTOR_INDEX_TOKENS.pop(tokenless_sector, None)

try:
    with patch(
        "data_feed.fetch_candles",
        side_effect=AssertionError("missing tokens must not fetch"),
    ):
        trend, reason = market_trend.get_sector_trend_diagnostic(
            kite,
            tokenless_symbol,
            cfg,
        )
finally:
    market_trend.SECTOR_MAP.pop(tokenless_symbol, None)
    market_trend.SECTOR_INDEX_TOKENS.pop(tokenless_sector, None)

check(
    "Mapped sectors without tokens have an explicit reason",
    trend == "Sideways" and reason == "MISSING_TOKEN",
)


with patch(
    "data_feed.fetch_candles",
    side_effect=RuntimeError("API unavailable"),
):
    legacy_market = market_trend.get_market_trend(kite, cfg)
    legacy_sector = market_trend.get_sector_trend(
        kite,
        "HDFCBANK",
        cfg,
    )

check(
    "Existing trend wrappers retain their string-only contract",
    legacy_market == "Sideways"
    and legacy_sector == "Sideways"
    and isinstance(legacy_market, str)
    and isinstance(legacy_sector, str),
)


expected_alignments = {
    ("BUY", "Bullish", "Bullish"): "STRONG_ALIGNMENT",
    ("BUY", "Bullish", "Sideways"): "ALIGNED",
    ("BUY", "Sideways", "Sideways"): "NEUTRAL",
    ("BUY", "Bearish", "Sideways"): "MISALIGNED",
    ("BUY", "Bearish", "Bearish"): "STRONG_MISALIGNMENT",
    ("SELL", "Bearish", "Bearish"): "STRONG_ALIGNMENT",
    ("SELL", "Bullish", "Bullish"): "STRONG_MISALIGNMENT",
}

check(
    "Five-level market-alignment decisions are unchanged",
    all(
        market_trend.compute_market_alignment(*inputs) == expected
        for inputs, expected in expected_alignments.items()
    ),
)


print("MARKET ALIGNMENT DIAGNOSTIC TESTS PASSED")
