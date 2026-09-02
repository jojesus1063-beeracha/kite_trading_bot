import tempfile
import time
import unittest
import inspect
from pathlib import Path
from types import SimpleNamespace

from equity_socket_shadow import SocketShadowEngine, _book_snapshot


class MemoryRecorder:
    def __init__(self):
        self.rows = []
        self.dropped = 0
        self.healthy = True

    def write(self, row):
        self.rows.append(row)

    def close(self):
        return None


def config(**changes):
    values = dict(
        PAPER_TRADING=True,
        SOCKET_SHADOW_RECORD_RAW_TICKS=True,
        SOCKET_SHADOW_OBSERVATION_SECONDS=30,
        SOCKET_SHADOW_MIN_WINDOW_SAMPLES=15,
        SOCKET_SHADOW_MIN_COVERAGE_SECONDS=29,
        SOCKET_SHADOW_IMBALANCE_THRESHOLD=.25,
        SOCKET_SHADOW_PERSISTENCE_RATIO=.75,
        SOCKET_SHADOW_DIRECTIONAL_TICK_RATIO=.60,
        SOCKET_SHADOW_MIN_PRICE_MOVE_PCT=.05,
        SOCKET_SHADOW_MAX_EXTENSION_PCT=.20,
        SOCKET_SHADOW_QUOTE_MAX_AGE_SECONDS=.5,
        SOCKET_SHADOW_TICK_MAX_AGE_SECONDS=5,
        SOCKET_SHADOW_STALE_CONSECUTIVE_CHECKS=2,
        SOCKET_SHADOW_MIN_DEPLETION_CYCLES=2,
        SOCKET_SHADOW_MIN_REPLENISHMENT_CYCLES=2,
        SOCKET_SHADOW_EVALUATION_SECONDS=10_000_000_000,
        SOCKET_SHADOW_MAX_SPREAD_PCT=.5,
        SOCKET_SHADOW_MAX_SLIPPAGE_PCT=.15,
        SOCKET_SHADOW_MAX_TRADES_PER_SYMBOL=1,
        SOCKET_SHADOW_MAX_TRADES_PER_DAY=100,
        SOCKET_SHADOW_MAX_OPEN_POSITIONS=5,
        SOCKET_SHADOW_STOP_PERCENT=.45,
        SOCKET_SHADOW_SCALP_R=1,
        SOCKET_SHADOW_RUNNER_R=2,
        SOCKET_SHADOW_SCALP_FRACTION=.5,
        SOCKET_SHADOW_MAX_DAILY_LOSS_PCT=None,
        NO_ENTRY_BEFORE="00:00",
        NO_ENTRY_AFTER="23:59",
        CAPITAL=5000,
        SOCKET_SHADOW_CAPITAL=5000,
        RISK_PER_TRADE_PCT=2,
        MAX_POSITION_SIZE_PCT=100,
        SOCKET_SHADOW_EXECUTABLE_DEPTH_MULTIPLE=2,
        SOCKET_SHADOW_DYNAMIC_EXIT_OBSERVATIONAL=False,
        SOCKET_SHADOW_DYNAMIC_EXIT_AUTHORITATIVE=True,
        SOCKET_SHADOW_FIXED_TARGETS_ENABLED=False,
        SOCKET_SHADOW_STALE_FALLBACK_STOP_ENABLED=True,
        SOCKET_SHADOW_STALE_FALLBACK_STOP_PERCENT=.45,
        SOCKET_SHADOW_STALE_FALLBACK_HYBRID_ENABLED=True,
        SOCKET_SHADOW_STALE_REST_POLL_SECONDS=2,
        SOCKET_SHADOW_DYNAMIC_EXIT_WINDOW_SECONDS=10,
        SOCKET_SHADOW_DYNAMIC_EXIT_PERSIST_SECONDS=10,
        SOCKET_SHADOW_DYNAMIC_EXIT_IMBALANCE=.15,
        SOCKET_SHADOW_DYNAMIC_EXIT_TICK_RATIO=.60,
        SOCKET_SHADOW_DYNAMIC_EXIT_MIN_DEPLETION=2,
        SOCKET_SHADOW_DYNAMIC_EXIT_MIN_REPLENISHMENT=2,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def tick(at, price, volume, bid_qty=1000, ask_qty=100, spread=.04):
    return {
        "instrument_token": 1,
        "exchange_timestamp": "2026-08-26T09:30:00+05:30",
        "last_price": price,
        "last_quantity": 10,
        "volume_traded": volume,
        "received_at": at,
        "depth": {
            "buy": [{"price": price - spread, "quantity": bid_qty, "orders": 1}] * 5,
            "sell": [{"price": price, "quantity": ask_qty, "orders": 1}] * 5,
        },
    }


class SocketShadowTests(unittest.TestCase):
    def make_engine(self, **changes):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return SocketShadowEngine(config(**changes), recorder=MemoryRecorder(), output_dir=Path(self.temp.name))

    def test_live_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "PAPER_TRADING"):
                SocketShadowEngine(config(PAPER_TRADING=False), recorder=MemoryRecorder(), output_dir=Path(directory))

    def test_persistent_buyer_flow_opens_paper_shadow_buy(self):
        engine = self.make_engine()
        start = time.time() - 31
        volume = 1000
        for index in range(77):
            volume += 100
            bid_qty, ask_qty = ((1200, 100) if index % 2 else (1000, 200))
            engine.on_tick("TEST", tick(start + index * .4, 100 + index * .0015, volume, bid_qty, ask_qty))
        metrics = engine.direction_metrics("TEST", start + 76 * .4)
        self.assertEqual("BUY", metrics["direction"])
        engine.cfg.SOCKET_SHADOW_EVALUATION_SECONDS = 0
        book = _book_snapshot(engine.states["TEST"].previous_tick)
        engine._maybe_evaluate_entry("TEST", book, start + 76 * .4)
        self.assertIn("TEST", engine.positions)
        self.assertEqual("BUY", engine.positions["TEST"].direction)

    def test_fresh_flow_does_not_use_fixed_targets(self):
        engine = self.make_engine()
        start = time.time() - 31
        volume = 1000
        for index in range(77):
            volume += 100
            bid_qty, ask_qty = ((1200, 100) if index % 2 else (1000, 200))
            engine.on_tick("TEST", tick(start + index * .4, 100 + index * .0015, volume, bid_qty, ask_qty))
        engine.cfg.SOCKET_SHADOW_EVALUATION_SECONDS = 0
        engine._maybe_evaluate_entry(
            "TEST", _book_snapshot(engine.states["TEST"].previous_tick), start + 76 * .4
        )
        position = engine.positions["TEST"]
        engine._dynamic_adverse_confirmed = lambda *args: (False, {"test": True})
        engine.on_tick("TEST", tick(start + 31, position.runner_target + .05, volume + 100))
        self.assertIn("TEST", engine.positions)

    def test_raw_record_contains_depth(self):
        engine = self.make_engine()
        engine.on_tick("TEST", tick(time.time(), 100, 1000))
        row = engine.recorder.rows[0]
        self.assertIn("depth", row)
        self.assertEqual(5, len(row["depth"]["buy"]))
        self.assertEqual(5, len(row["depth"]["sell"]))

    def test_engine_has_no_broker_order_call(self):
        import equity_socket_shadow
        source = inspect.getsource(equity_socket_shadow)
        self.assertNotIn("place_order(", source)
        self.assertNotIn("modify_order(", source)
        self.assertNotIn("cancel_order(", source)

    def test_stale_quote_keeps_aggressor_unknown(self):
        engine = self.make_engine()
        previous = tick(100.0, 100.0, 1000)
        self.assertEqual("UNKNOWN", engine._classify_aggressor(101.0, previous, 100.6))

    def test_liquidity_caps_quantity(self):
        engine = self.make_engine()
        metrics = {"direction": "BUY", "strict_pass": True}
        book = {
            "best_bid": 99.96, "best_ask": 100.0, "bid_qty": 100,
            "ask_qty": 10, "spread_pct": .04,
        }
        engine.states["TEST"].points.append(SimpleNamespace(price=100.0))
        engine._open_position("TEST", "BUY", book, metrics, time.time())
        self.assertEqual(5, engine.positions["TEST"].quantity)

    def test_daily_loss_limit_is_disabled(self):
        engine = self.make_engine()
        self.assertIsNone(engine.cfg.SOCKET_SHADOW_MAX_DAILY_LOSS_PCT)

    def test_dynamic_exit_is_authoritative_while_fresh(self):
        engine = self.make_engine()
        metrics = {"direction": "BUY", "strict_pass": True}
        book = {
            "best_bid": 99.96, "best_ask": 100.0, "bid_qty": 1000,
            "ask_qty": 1000, "spread_pct": .04,
        }
        engine.states["TEST"].points.append(SimpleNamespace(price=100.0))
        start = time.time()
        engine._open_position("TEST", "BUY", book, metrics, start)
        self.assertIn("TEST", engine.positions)
        engine._dynamic_adverse_confirmed = lambda *args: (True, {"test": True})
        engine._manage_authoritative_exit("TEST", 100.0, book, start)
        engine._manage_authoritative_exit("TEST", 100.0, book, start + 11)
        self.assertNotIn("TEST", engine.positions)

    def test_stale_fallback_latches_and_runs_hybrid_1r_2r(self):
        engine = self.make_engine()
        metrics = {"direction": "BUY", "strict_pass": True}
        book = {
            "best_bid": 99.96, "best_ask": 100.0, "bid_qty": 1000,
            "ask_qty": 1000, "spread_pct": .04,
        }
        start = time.time()
        engine.states["TEST"].points.append(SimpleNamespace(price=100.0, received_at=start,
                                                              best_bid=99.96, best_ask=100.0))
        engine._open_position("TEST", "BUY", book, metrics, start)
        position = engine.positions["TEST"]
        self.assertEqual([], engine.arm_stale_fallbacks(start + 6))
        self.assertEqual(["TEST"], engine.arm_stale_fallbacks(start + 7))
        self.assertIn("TEST", engine.fallback_armed)
        engine.on_fallback_quote("TEST", {
            "last_price": position.scalp_target,
            "depth": {"buy": [{"price": position.scalp_target}],
                      "sell": [{"price": position.scalp_target + .05}]},
        }, start + 4)
        self.assertEqual(0, position.scalp_remaining)
        self.assertEqual(position.entry, position.stop)
        engine.on_fallback_quote("TEST", {
            "last_price": position.runner_target,
            "depth": {"buy": [{"price": position.runner_target}],
                      "sell": [{"price": position.runner_target + .05}]},
        }, start + 5)
        self.assertNotIn("TEST", engine.positions)
        self.assertGreater(engine.realized_net, 0)

    def test_stale_fallback_stop_uses_rest_quote(self):
        engine = self.make_engine()
        metrics = {"direction": "BUY", "strict_pass": True}
        book = {"best_bid": 99.96, "best_ask": 100.0, "bid_qty": 1000,
                "ask_qty": 1000, "spread_pct": .04}
        start = time.time()
        engine.states["TEST"].points.append(SimpleNamespace(price=100.0, received_at=start,
                                                              best_bid=99.96, best_ask=100.0))
        engine._open_position("TEST", "BUY", book, metrics, start)
        stop = engine.positions["TEST"].stop
        engine.arm_stale_fallbacks(start + 1, "WEBSOCKET_DISCONNECTED")
        engine.on_fallback_quote("TEST", {"last_price": stop,
            "depth": {"buy": [{"price": stop}], "sell": [{"price": stop + .05}]}}, start + 2)
        self.assertNotIn("TEST", engine.positions)
        self.assertLess(engine.realized_net, 0)


if __name__ == "__main__":
    unittest.main()
