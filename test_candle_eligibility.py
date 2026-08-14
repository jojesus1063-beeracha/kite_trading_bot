import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from candle_eligibility import evaluate_candle_eligibility


class FakeTalib:
    def __init__(self, scores=None):
        self.scores = scores or {}

    def __getattr__(self, name):
        if not name.startswith("CDL"):
            raise AttributeError(name)

        def detector(open_, high, low, close):
            result = np.zeros(len(close), dtype=int)
            result[-1] = self.scores.get(name, 0)
            return result

        return detector


def config():
    return SimpleNamespace(
        ENTRY_TIMEFRAME="3minute",
        PAPER_CANDLE_MAX_FRESH_SECONDS=90.0,
        PAPER_CANDLE_COMPLETION_GRACE_SECONDS=5.0,
        PAPER_CANDLE_VOLUME_LOOKBACK=20,
        PAPER_CANDLE_MIN_VOLUME_RATIO=1.2,
        PAPER_CANDLE_REQUIRED_CONFIRMATIONS=2,
        PAPER_REQUIRE_EMA200_ALIGNMENT=False,
        PAPER_ENABLE_COST_AWARE_GATE=True,
        PAPER_COST_MOVE_LOOKBACK=14,
        PAPER_EXPECTED_MOVE_ATR_MULTIPLIER=1.0,
        PAPER_MIN_EXPECTED_GROSS_TO_COST_MULTIPLE=2.0,
        CAPITAL=5000.0,
        RISK_PER_TRADE_PCT=0.20,
        STOP_LOSS_PERCENT=0.45,
        PAPER_BUY_MIN_ADX=25.0,
        PAPER_SELL_MIN_ADX=20.0,
    )


def entry_frame(direction="BUY"):
    start = 100.0 if direction == "BUY" else 110.0
    end = 110.0 if direction == "BUY" else 100.0
    close = np.linspace(start, end, 25)
    frame = pd.DataFrame({
        "date": pd.date_range("2026-08-13 08:48", periods=25, freq="3min", tz="Asia/Kolkata"),
        "open": close - 0.1 if direction == "BUY" else close + 0.1,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": [100.0] * 24 + [200.0],
        "vwap": [95.0] * 25 if direction == "BUY" else [115.0] * 25,
    })
    return frame


def trend_frame(direction="BUY", adx=30.0):
    if direction == "BUY":
        return pd.DataFrame([{"close": 110.0, "ema200": 90.0, "adx": adx}])
    return pd.DataFrame([{"close": 100.0, "ema200": 120.0, "adx": adx}])


class CandleEligibilityTests(unittest.TestCase):
    def test_accepts_confluent_buy(self):
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=0.0,
        )
        self.assertTrue(result.accepted, result.to_dict())

    def test_buy_requires_stronger_adx(self):
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend_frame("BUY", adx=22.0), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=0.0,
        )
        self.assertFalse(result.accepted)
        self.assertIn("ADX_STRENGTH_BELOW_MINIMUM_OR_UNAVAILABLE", result.reasons)

    def test_accepts_confluent_sell_at_adx_20_plus(self):
        result = evaluate_candle_eligibility(
            entry_frame("SELL"), trend_frame("SELL", adx=22.0), "SELL", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": -100}),
            price_action_score=0.0,
        )
        self.assertTrue(result.accepted, result.to_dict())

    def test_conflicting_tier1_patterns_reject(self):
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({
                "CDLENGULFING": 100,
                "CDLSHOOTINGSTAR": -100,
            }),
            price_action_score=10.0,
        )
        self.assertFalse(result.accepted)
        self.assertIn("CONFLICTING_TIER1_PATTERNS", result.reasons)

    def test_stale_candle_rejects(self):
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:06:00+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=0.0,
        )
        self.assertFalse(result.accepted)
        self.assertIn("CANDLE_NOT_COMPLETED_OR_FRESH", result.reasons)


    def test_accepts_volume_plus_price_action_without_tier1_pattern(self):
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib(),
            price_action_score=10.0,
        )
        self.assertTrue(result.accepted, result.to_dict())
        self.assertEqual(result.detail["confirmation_count"], 2)

    def test_accepts_pattern_plus_price_action_without_volume_spike(self):
        frame = entry_frame("BUY")
        frame.loc[frame.index[-1], "volume"] = 110.0
        result = evaluate_candle_eligibility(
            frame, trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=10.0,
        )
        self.assertTrue(result.accepted, result.to_dict())

    def test_rejects_when_only_one_confirmation_is_present(self):
        frame = entry_frame("BUY")
        frame.loc[frame.index[-1], "volume"] = 110.0
        result = evaluate_candle_eligibility(
            frame, trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib(),
            price_action_score=10.0,
        )
        self.assertFalse(result.accepted)
        self.assertIn("INSUFFICIENT_ENTRY_CONFIRMATIONS", result.reasons)


    def test_ema200_misalignment_is_observational(self):
        trend = pd.DataFrame([{"close": 80.0, "ema200": 90.0, "adx": 30.0}])
        result = evaluate_candle_eligibility(
            entry_frame("BUY"), trend, "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=0.0,
        )
        self.assertTrue(result.accepted, result.to_dict())
        self.assertFalse(result.detail["ema200_aligned"])
        self.assertFalse(result.detail["ema200_alignment_required"])

    def test_cost_gate_rejects_tiny_expected_movement(self):
        frame = entry_frame("BUY")
        frame["open"] = 100.0
        frame["high"] = 100.001
        frame["low"] = 99.999
        frame["close"] = 100.0
        frame["vwap"] = 95.0
        result = evaluate_candle_eligibility(
            frame, trend_frame("BUY"), "BUY", config(),
            now="2026-08-13 10:03:12+05:30",
            talib_module=FakeTalib({"CDLENGULFING": 100}),
            price_action_score=0.0,
        )
        self.assertFalse(result.accepted)
        self.assertIn("EXPECTED_MOVE_DOES_NOT_COVER_COSTS", result.reasons)


if __name__ == "__main__":
    unittest.main()
