import unittest

import pandas as pd
from types import SimpleNamespace

from breakout_validator import validate_breakout
from price_action import get_price_action_score


def frame(current, *, prior_volume=100.0):
    rows = []
    for index in range(20):
        close = 99.5 + (index % 3) * 0.1
        rows.append({
            "open": close,
            "high": 100.0,
            "low": 99.0,
            "close": close,
            "volume": prior_volume,
        })
    rows.append(current)
    return pd.DataFrame(rows)


class BreakoutValidatorTests(unittest.TestCase):
    def test_valid_high_volume_bullish_expansion(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 102.4, "low": 99.0,
            "close": 102.1, "volume": 180.0,
        }), "BUY")
        self.assertTrue(result.passed, result.to_dict())
        self.assertGreaterEqual(result.metrics["volume_ratio"], 1.5)
        self.assertGreaterEqual(result.metrics["atr_multiplier"], 1.2)
        self.assertGreaterEqual(result.metrics["clv"], 0.60)

    def test_valid_high_volume_bearish_expansion(self):
        result = validate_breakout(frame({
            "open": 99.0, "high": 101.0, "low": 97.6,
            "close": 97.9, "volume": 180.0,
        }), "SELL")
        self.assertTrue(result.passed, result.to_dict())
        self.assertLessEqual(result.metrics["clv"], -0.60)

    def test_overextended_breakout_is_rejected(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 104.0, "low": 99.0,
            "close": 103.5, "volume": 180.0,
        }), "BUY")

        self.assertFalse(result.passed)
        self.assertIn("BREAKOUT_OVEREXTENDED_ATR", result.reasons)
        self.assertFalse(result.metrics["not_overextended"])
        self.assertGreater(
            result.metrics["atr_multiplier"],
            result.metrics["maximum_atr_multiplier"],
        )


    def test_low_volume_fakeout_is_rejected_with_metrics(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 104.0, "low": 99.0,
            "close": 103.5, "volume": 149.0,
        }), "BUY")
        self.assertFalse(result.passed)
        self.assertIn("VOLUME_RATIO_BELOW_MINIMUM", result.reasons)
        self.assertAlmostEqual(result.metrics["volume_ratio"], 1.49)
        self.assertIsNotNone(result.metrics["n_period_high"])
        self.assertIsNotNone(result.metrics["atr_multiplier"])
        self.assertIsNotNone(result.metrics["clv"])

    def test_bullish_clv_boundary_passes(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 105.0, "low": 100.0,
            "close": 104.0, "volume": 200.0,
        }), "BUY")
        self.assertTrue(result.metrics["clv_confirmed"])
        self.assertAlmostEqual(result.metrics["clv"], 0.60)

    def test_bullish_clv_below_boundary_rejects(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 105.0, "low": 100.0,
            "close": 103.99, "volume": 200.0,
        }), "BUY")
        self.assertFalse(result.metrics["clv_confirmed"])
        self.assertIn("CLV_DIRECTION_NOT_CONFIRMED", result.reasons)

    def test_bearish_clv_boundary_passes(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 100.0, "low": 95.0,
            "close": 96.0, "volume": 200.0,
        }), "SELL")
        self.assertTrue(result.metrics["clv_confirmed"])
        self.assertAlmostEqual(result.metrics["clv"], -0.60)

    def test_zero_range_candle_rejects_clv_without_crashing(self):
        result = validate_breakout(frame({
            "open": 101.0, "high": 101.0, "low": 101.0,
            "close": 101.0, "volume": 200.0,
        }), "BUY")
        self.assertFalse(result.passed)
        self.assertIsNone(result.metrics["clv"])
        self.assertIn("CLV_DIRECTION_NOT_CONFIRMED", result.reasons)

    def test_insufficient_history_fails_closed(self):
        result = validate_breakout(frame({
            "open": 100.0, "high": 104.0, "low": 99.0,
            "close": 103.5, "volume": 180.0,
        }).tail(20), "BUY")
        self.assertFalse(result.passed)
        self.assertEqual(result.reasons, ["INSUFFICIENT_HISTORY"])

    def test_price_action_pipeline_exposes_complete_telemetry(self):
        cfg = SimpleNamespace(
            USE_MARKET_STRUCTURE=False,
            USE_SUPPORT_RESISTANCE=False,
            USE_BREAKOUT_CONFIRMATION=True,
            USE_PULLBACK_ENTRY=False,
            USE_REJECTION_CANDLES=False,
            USE_BOS=False,
            USE_RANGE_FILTER=False,
            USE_CHOCH=False,
            BREAKOUT_LOOKBACK=20,
            BREAKOUT_VOLUME_PERIOD=20,
            BREAKOUT_MIN_VOLUME_RATIO=1.5,
            BREAKOUT_ATR_PERIOD=14,
            BREAKOUT_MIN_ATR_MULTIPLIER=1.2,
            BREAKOUT_MAX_ATR_MULTIPLIER=3.0,
            BREAKOUT_CLV_THRESHOLD=0.60,
        )
        score, detail = get_price_action_score(frame({
            "open": 100.0, "high": 102.4, "low": 99.0,
            "close": 102.1, "volume": 180.0,
        }), "BUY", cfg)
        telemetry = detail["breakout_validation"]
        self.assertEqual(score, 10)
        self.assertTrue(telemetry["passed"])
        for metric in (
            "n_period_high", "n_period_low", "volume_ratio",
            "atr_multiplier", "clv",
        ):
            self.assertIn(metric, telemetry["metrics"])


if __name__ == "__main__":
    unittest.main()
