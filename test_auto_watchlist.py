#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from auto_watchlist import (
    AutoWatchlistError,
    SelectorSettings,
    evaluate_quote,
    generate_selection,
    parse_nifty500_csv,
    usable_nse_equity_instruments,
    write_watchlist_to_config,
)


def make_universe_row(
    symbol: str,
    industry: str = "Test Industry",
) -> dict[str, str]:
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Ltd.",
        "industry": industry,
        "series": "EQ",
        "isin": f"INE{symbol}",
    }


def make_quote(
    *,
    token: int = 1,
    last_price: float = 100.0,
    volume: int = 100_000,
    average_price: float = 100.0,
    open_price: float = 99.0,
    high_price: float = 102.0,
    low_price: float = 98.0,
    previous_close: float = 100.0,
    bid: float = 99.95,
    ask: float = 100.05,
) -> dict:
    depth = {
        "buy": (
            [{"price": bid, "quantity": 1000, "orders": 5}]
            if bid > 0
            else []
        ),
        "sell": (
            [{"price": ask, "quantity": 1000, "orders": 5}]
            if ask > 0
            else []
        ),
    }

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
        "depth": depth,
    }


class FakeKite:
    def __init__(self, symbols, quotes):
        self._symbols = symbols
        self._quotes = quotes
        self.quote_calls = []

    def instruments(self, exchange):
        if exchange != "NSE":
            raise AssertionError("Expected NSE instrument request")

        return [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
                "instrument_token": index + 1,
            }
            for index, symbol in enumerate(self._symbols)
        ]

    def quote(self, keys):
        self.quote_calls.append(list(keys))
        return {
            key: self._quotes[key]
            for key in keys
            if key in self._quotes
        }


class AutoWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.settings = SelectorSettings(
            top_n=3,
            min_selected=1,
            min_price=20.0,
            max_price=5000.0,
            min_turnover=500_000.0,
            max_spread_pct=0.40,
            min_circuit_distance_pct=0.75,
        )

    def test_parse_nifty_csv_keeps_only_unique_eq_rows(self):
        csv_text = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Alpha Ltd.,IT,ALPHA,EQ,INE001\n"
            "Alpha Ltd.,IT,ALPHA,EQ,INE001\n"
            "Beta Ltd.,Finance,BETA,BE,INE002\n"
        )

        with self.assertRaises(AutoWatchlistError):
            parse_nifty500_csv(csv_text)

    def test_instrument_filter_keeps_only_nse_equities(self):
        instruments = [
            {
                "tradingsymbol": "ALPHA",
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
            },
            {
                "tradingsymbol": "FUTURE",
                "exchange": "NFO",
                "segment": "NFO-FUT",
                "instrument_type": "FUT",
            },
            {
                "tradingsymbol": "BETA",
                "exchange": "BSE",
                "segment": "BSE",
                "instrument_type": "EQ",
            },
        ]

        result = usable_nse_equity_instruments(instruments)

        self.assertEqual(set(result), {"ALPHA"})

    def test_eligible_quote_is_accepted(self):
        candidate, reason = evaluate_quote(
            make_universe_row("ALPHA"),
            make_quote(),
            self.settings,
        )

        self.assertIsNone(reason)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["symbol"], "ALPHA")
        self.assertGreater(candidate["score"], 0)

    def test_wide_spread_is_rejected(self):
        candidate, reason = evaluate_quote(
            make_universe_row("ALPHA"),
            make_quote(bid=99.0, ask=101.0),
            self.settings,
        )

        self.assertIsNone(candidate)
        self.assertEqual(reason, "spread_too_wide")

    def test_missing_depth_is_strict_by_default(self):
        candidate, reason = evaluate_quote(
            make_universe_row("ALPHA"),
            make_quote(bid=0, ask=0),
            self.settings,
        )

        self.assertIsNone(candidate)
        self.assertEqual(reason, "missing_market_depth")

    def test_missing_depth_can_be_allowed_for_dry_run(self):
        candidate, reason = evaluate_quote(
            make_universe_row("ALPHA"),
            make_quote(bid=0, ask=0),
            self.settings,
            allow_missing_depth=True,
        )

        self.assertIsNone(reason)
        self.assertIsNotNone(candidate)
        self.assertIsNone(candidate["spread_pct"])

    def test_generation_ranks_and_limits_candidates(self):
        universe = [
            make_universe_row("ALPHA"),
            make_universe_row("BETA"),
            make_universe_row("GAMMA"),
        ]

        quotes = {
            "NSE:ALPHA": make_quote(
                token=1,
                volume=100_000,
            ),
            "NSE:BETA": make_quote(
                token=2,
                volume=500_000,
            ),
            "NSE:GAMMA": make_quote(
                token=3,
                volume=200_000,
            ),
        }

        settings = SelectorSettings(
            top_n=2,
            min_selected=2,
            min_turnover=500_000.0,
        )

        result = generate_selection(
            FakeKite(
                ["ALPHA", "BETA", "GAMMA"],
                quotes,
            ),
            universe,
            settings,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [item["symbol"] for item in result["selected"]],
            ["BETA", "GAMMA"],
        )

    def test_configuration_write_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "user_config.json"
            runtime_dir = root / "runtime"

            original = {
                "capital": 5000.0,
                "paper_trading": False,
                "profit_target_percent": 0.7,
                "watchlist": [],
            }

            config_path.write_text(
                json.dumps(original),
                encoding="utf-8",
            )

            selected = [
                {"symbol": "ALPHA"},
                {"symbol": "BETA"},
            ]

            backup_path = write_watchlist_to_config(
                config_path,
                selected,
                min_selected=2,
                runtime_dir=runtime_dir,
            )

            updated = json.loads(
                config_path.read_text(encoding="utf-8")
            )

            self.assertEqual(updated["capital"], 5000.0)
            self.assertFalse(updated["paper_trading"])
            self.assertEqual(
                updated["watchlist"],
                [
                    {"symbol": "ALPHA", "exchange": "NSE"},
                    {"symbol": "BETA", "exchange": "NSE"},
                ],
            )
            self.assertTrue(backup_path.exists())

    def test_undersized_watchlist_is_never_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "user_config.json"
            runtime_dir = root / "runtime"

            original = {
                "capital": 5000.0,
                "watchlist": [],
            }

            config_path.write_text(
                json.dumps(original),
                encoding="utf-8",
            )

            with self.assertRaises(AutoWatchlistError):
                write_watchlist_to_config(
                    config_path,
                    [{"symbol": "ALPHA"}],
                    min_selected=2,
                    runtime_dir=runtime_dir,
                )

            unchanged = json.loads(
                config_path.read_text(encoding="utf-8")
            )

            self.assertEqual(unchanged, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
