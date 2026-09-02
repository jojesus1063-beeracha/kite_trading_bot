#!/usr/bin/env python3

import unittest

from auto_watchlist import (
    SelectorSettings,
    calculate_previous_day_momentum,
    evaluate_quote,
    generate_selection,
)


def universe_row(symbol, token, tick_size=0.05):
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Ltd.",
        "industry": "Test",
        "series": "EQ",
        "isin": f"INE{symbol}",
        "instrument_token": token,
        "tick_size": tick_size,
    }


def quote(
    *,
    token,
    last_price=100.0,
    volume=100_000,
    average_price=100.0,
    open_price=100.0,
    high_price=101.0,
    low_price=99.0,
    previous_close=100.0,
    bid=99.95,
    ask=100.05,
):
    return {
        "instrument_token": token,
        "last_price": last_price,
        "volume": volume,
        "average_price": average_price,
        "upper_circuit_limit": 120.0,
        "lower_circuit_limit": 80.0,
        "ohlc": {
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": previous_close,
        },
        "depth": {
            "buy": [
                {
                    "price": bid,
                    "quantity": 1000,
                    "orders": 5,
                }
            ],
            "sell": [
                {
                    "price": ask,
                    "quantity": 1000,
                    "orders": 5,
                }
            ],
        },
    }


def candles(previous_close, latest_close, latest_volume=200_000):
    return [
        {
            "date": "2026-07-30",
            "open": previous_close,
            "high": previous_close * 1.01,
            "low": previous_close * 0.99,
            "close": previous_close,
            "volume": 100_000,
        },
        {
            "date": "2026-07-31",
            "open": previous_close,
            "high": max(
                previous_close,
                latest_close,
            ) * 1.01,
            "low": min(
                previous_close,
                latest_close,
            ) * 0.99,
            "close": latest_close,
            "volume": latest_volume,
        },
    ]


class FakeKite:
    def __init__(self, symbols, quotes, histories):
        self.symbols = symbols
        self.quotes = quotes
        self.histories = histories

    def instruments(self, exchange):
        assert exchange == "NSE"

        return [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
                "instrument_token": index + 1,
                "tick_size": 0.05,
            }
            for index, symbol in enumerate(self.symbols)
        ]

    def quote(self, keys):
        return {
            key: self.quotes[key]
            for key in keys
            if key in self.quotes
        }

    def historical_data(
        self,
        instrument_token,
        from_date,
        to_date,
        interval,
        continuous=False,
        oi=False,
    ):
        assert interval == "day"
        return self.histories[instrument_token]


class PriorityTests(unittest.TestCase):
    def test_open_equals_low_is_strict_zero_ticks(self):
        settings = SelectorSettings(
            top_n=1,
            min_selected=1,
            min_turnover=1.0,
            open_low_tolerance_ticks=0,
        )

        candidate, reason = evaluate_quote(
            universe_row("STRICT", 1),
            quote(
                token=1,
                open_price=100.0,
                low_price=100.0,
                high_price=100.5,
                last_price=100.25,
            ),
            settings,
        )

        self.assertIsNone(reason)
        self.assertTrue(candidate["open_equals_low"])
        self.assertEqual(
            candidate["open_low_difference"],
            0.0,
        )

    def test_one_tick_below_open_is_not_open_equals_low(self):
        settings = SelectorSettings(
            top_n=1,
            min_selected=1,
            min_turnover=1.0,
            open_low_tolerance_ticks=0,
        )

        candidate, reason = evaluate_quote(
            universe_row("ONETICK", 1),
            quote(
                token=1,
                open_price=100.0,
                low_price=99.95,
                high_price=100.5,
                last_price=100.25,
            ),
            settings,
        )

        self.assertIsNone(reason)
        self.assertFalse(candidate["open_equals_low"])

    def test_previous_day_uses_absolute_momentum(self):
        positive = calculate_previous_day_momentum(
            candles(100.0, 105.0)
        )
        negative = calculate_previous_day_momentum(
            candles(100.0, 93.0)
        )

        self.assertEqual(
            positive["previous_day_direction"],
            "UP",
        )
        self.assertEqual(
            negative["previous_day_direction"],
            "DOWN",
        )
        self.assertGreater(
            negative["previous_day_momentum_score"],
            positive["previous_day_momentum_score"],
        )

    def test_final_order_uses_all_three_priorities(self):
        symbols = ["OPENLOW", "LIVE", "FALLBACK"]

        quotes = {
            "NSE:OPENLOW": quote(
                token=1,
                open_price=100.0,
                low_price=100.0,
                high_price=100.10,
                last_price=100.05,
            ),
            "NSE:LIVE": quote(
                token=2,
                open_price=100.0,
                low_price=99.0,
                high_price=103.0,
                last_price=102.0,
            ),
            "NSE:FALLBACK": quote(
                token=3,
                open_price=100.0,
                low_price=99.95,
                high_price=100.05,
                last_price=100.0,
            ),
        }

        histories = {
            1: candles(100.0, 100.5),
            2: candles(100.0, 102.0),
            3: candles(100.0, 108.0),
        }

        settings = SelectorSettings(
            top_n=3,
            min_selected=3,
            min_turnover=1.0,
            min_live_momentum_pct=0.20,
            historical_delay_seconds=0.34,
        )

        result = generate_selection(
            FakeKite(
                symbols,
                quotes,
                histories,
            ),
            [
                universe_row("OPENLOW", 1),
                universe_row("LIVE", 2),
                universe_row("FALLBACK", 3),
            ],
            settings,
        )

        self.assertEqual(result["status"], "success")

        self.assertEqual(
            [item["symbol"] for item in result["selected"]],
            ["OPENLOW", "LIVE", "FALLBACK"],
        )

        self.assertEqual(
            [
                item["selection_priority"]
                for item in result["selected"]
            ],
            [
                "PRIORITY_1_OPEN_EQUALS_LOW",
                "PRIORITY_2_LIVE_MOMENTUM",
                "PRIORITY_3_PREVIOUS_DAY_MOMENTUM",
            ],
        )

    def test_fallback_contains_no_duplicates(self):
        symbols = ["A", "B", "C", "D"]

        quotes = {
            f"NSE:{symbol}": quote(
                token=index,
                open_price=100.0,
                low_price=99.95,
                high_price=100.05,
                last_price=100.0,
            )
            for index, symbol in enumerate(symbols, 1)
        }

        histories = {
            index: candles(
                100.0,
                100.0 + index,
            )
            for index in range(1, 5)
        }

        settings = SelectorSettings(
            top_n=4,
            min_selected=4,
            min_turnover=1.0,
            min_live_momentum_pct=1.0,
            historical_delay_seconds=0.34,
        )

        result = generate_selection(
            FakeKite(
                symbols,
                quotes,
                histories,
            ),
            [
                universe_row(symbol, index)
                for index, symbol
                in enumerate(symbols, 1)
            ],
            settings,
        )

        selected_symbols = [
            item["symbol"]
            for item in result["selected"]
        ]

        self.assertEqual(len(selected_symbols), 4)
        self.assertEqual(
            len(selected_symbols),
            len(set(selected_symbols)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
