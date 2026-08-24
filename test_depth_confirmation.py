import types
import unittest

from depth_confirmation import evaluate_depth_ticks, evaluate_live_depth
from ws_ticker import TickBuffer


def tick(at, buy, sell, bid=100.0, ask=100.02):
    return {
        "received_at": at,
        "depth": {
            "buy": [{"price": bid - i * 0.01, "quantity": buy // 5} for i in range(5)],
            "sell": [{"price": ask + i * 0.01, "quantity": sell // 5} for i in range(5)],
        },
    }


class DepthConfirmationTests(unittest.TestCase):
    def samples(self, buy, sell):
        return [tick(1000 + second, buy, sell) for second in range(31)]

    def evaluate(self, ticks, direction="BUY", quantity=10):
        return evaluate_depth_ticks(ticks, direction, quantity, now=1030)

    def test_persistent_opposition_skips_but_never_reverses(self):
        result = self.evaluate(self.samples(700, 1400), "BUY")
        self.assertFalse(result.accepted)
        self.assertEqual(result.classification, "OPPOSING")
        self.assertLess(result.median_imbalance, -0.20)
        self.assertIn("opposes BUY", result.reason)

    def test_persistent_support_confirms(self):
        result = self.evaluate(self.samples(1400, 700), "BUY")
        self.assertTrue(result.accepted)
        self.assertEqual(result.classification, "CONFIRMED")

    def test_mixed_depth_keeps_existing_direction(self):
        ticks = [
            tick(1000 + second, 1400, 700) if second % 2 == 0
            else tick(1000 + second, 700, 1400)
            for second in range(31)
        ]
        result = self.evaluate(ticks, "SELL")
        self.assertTrue(result.accepted)
        self.assertEqual(result.classification, "NEUTRAL")

    def test_fails_closed_on_stale_or_short_history(self):
        stale = self.evaluate([tick(1000 + second, 1400, 700) for second in range(11)])
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.classification, "STALE")

        short = evaluate_depth_ticks(
            [tick(1028 + second, 1400, 700) for second in range(3)],
            "BUY",
            10,
            now=1030,
        )
        self.assertFalse(short.accepted)
        self.assertEqual(short.classification, "INSUFFICIENT_HISTORY")

    def test_rejects_wide_spread_and_insufficient_executable_depth(self):
        wide = [tick(1000 + second, 1400, 700, bid=100, ask=100.10) for second in range(31)]
        result = self.evaluate(wide)
        self.assertFalse(result.accepted)
        self.assertEqual(result.classification, "WIDE_SPREAD")

        thin = self.evaluate(self.samples(1400, 10), "BUY", quantity=10)
        self.assertFalse(thin.accepted)
        self.assertEqual(thin.classification, "INSUFFICIENT_DEPTH")

    def test_live_adapter_reads_only_recent_buffered_ticks(self):
        buffer = TickBuffer()
        buffer.append("HCLTECH", tick(900, 1400, 700))
        for item in self.samples(1400, 700):
            buffer.append("HCLTECH", item)
        engine = types.SimpleNamespace(
            ws_ticker=types.SimpleNamespace(tick_buffer=buffer)
        )
        cfg = types.SimpleNamespace(
            ENABLE_DEPTH_CONFIRMATION_GATE=True,
            DEPTH_CONFIRMATION_WINDOW_SECONDS=30.0,
        )
        result = evaluate_live_depth(
            engine, "HCLTECH", "BUY", 10, cfg, now=1030
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.classification, "CONFIRMED")
        self.assertEqual(result.sample_count, 31)

    def test_live_adapter_fails_closed_without_websocket(self):
        cfg = types.SimpleNamespace(ENABLE_DEPTH_CONFIRMATION_GATE=True)
        result = evaluate_live_depth(None, "HCLTECH", "BUY", 10, cfg)
        self.assertFalse(result.accepted)
        self.assertEqual(result.classification, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
