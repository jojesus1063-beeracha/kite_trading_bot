"""
KiteTicker (WebSocket) lifecycle wrapper: connect, subscribe, health
tracking, and reconnection -- first use of KiteTicker anywhere in this
codebase (the equity bot is 100% REST-polling; see architecture review
Finding 1). Kept deliberately thin: all it does is turn broker
WebSocket events into NormalizedTick updates on a TickStore, and track
connection-quality state for disconnect handling (spec #26).

Deliberately does NOT contain any strategy/trading logic -- state_machine.py
reads TickStore and FnoTicker.is_connected()/last_disconnect_at, it
never reaches into KiteTicker internals directly.
"""
import time
import logging
import threading
from typing import Optional

from fno_bot.market_data.tick_store import TickStore, normalize_kite_tick

logger = logging.getLogger("fno.ticker")


class FnoTicker:
    def __init__(self, api_key: str, access_token: str, tick_store: TickStore = None,
                 kite_ticker_cls=None, clock_fn=None):
        """
        kite_ticker_cls: injectable KiteTicker class (defaults to
        kiteconnect.KiteTicker) so tests can pass a fake instead of
        opening a real WebSocket connection.
        """
        if kite_ticker_cls is None:
            from kiteconnect import KiteTicker as kite_ticker_cls  # local import: this module must be
                                                                      # importable even where kiteconnect
                                                                      # isn't installed (e.g. pure unit
                                                                      # tests of TickStore/strategy logic)
        self._clock_fn = clock_fn or time.monotonic
        self.tick_store = tick_store or TickStore(clock_fn=self._clock_fn)

        self._connected = threading.Event()
        self._subscribed_tokens: set[int] = set()
        self._pending_subscribe_tokens: set[int] = set()
        self.last_connect_at: Optional[float] = None
        self.last_disconnect_at: Optional[float] = None
        self.connect_count = 0
        self.disconnect_count = 0
        self._on_connect_hooks = []

        self.kws = kite_ticker_cls(api_key, access_token)
        self.kws.on_ticks = self._on_ticks
        self.kws.on_connect = self._on_connect
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect
        self.kws.on_noreconnect = self._on_noreconnect

    # -- lifecycle -----------------------------------------------------

    def connect(self, threaded: bool = True):
        """Opens the WebSocket connection. threaded=True (default) so
        the caller's PREPARE flow isn't blocked -- ticks arrive on a
        background thread via _on_ticks."""
        self.kws.connect(threaded=threaded)

    def close(self):
        try:
            self.kws.close()
        except Exception as e:
            logger.warning(f"FnoTicker.close() raised during shutdown: {e}")

    def subscribe(self, tokens: list[int], mode: str = "full"):
        """
        Subscribes to the given instrument tokens in the given mode
        ("full" gives market depth, needed for bid/ask/spread -- spec
        requires depth for entry/exit pricing). If not yet connected,
        the tokens are queued and subscribed automatically on connect.
        """
        tokens = list(tokens)
        if self.is_connected():
            self.kws.subscribe(tokens)
            self.kws.set_mode(mode, tokens)
            self._subscribed_tokens.update(tokens)
            logger.info(f"SUBSCRIPTION_OK tokens={tokens} mode={mode}")
        else:
            self._pending_subscribe_tokens.update(tokens)
            self._pending_mode = mode

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout_seconds: float) -> bool:
        return self._connected.wait(timeout=timeout_seconds)

    def on_connect_hook(self, fn):
        """Register an extra callback invoked (with this FnoTicker) on
        every successful connect/reconnect -- used by the state machine
        to know when to re-subscribe or resume monitoring after a drop."""
        self._on_connect_hooks.append(fn)

    # -- KiteTicker callbacks -------------------------------------------

    def _on_connect(self, ws, response):
        self._connected.set()
        self.last_connect_at = self._clock_fn()
        self.connect_count += 1
        logger.info(f"WEBSOCKET_READY connect_count={self.connect_count}")
        if self._pending_subscribe_tokens:
            tokens = list(self._pending_subscribe_tokens)
            mode = getattr(self, "_pending_mode", "full")
            ws.subscribe(tokens)
            ws.set_mode(mode, tokens)
            self._subscribed_tokens.update(tokens)
            self._pending_subscribe_tokens.clear()
            logger.info(f"SUBSCRIPTION_OK tokens={tokens} mode={mode}")
        for hook in self._on_connect_hooks:
            try:
                hook(self)
            except Exception as e:
                logger.error(f"on_connect_hook raised: {e}")

    def _on_close(self, ws, code, reason):
        self._connected.clear()
        self.last_disconnect_at = self._clock_fn()
        self.disconnect_count += 1
        logger.warning(f"WEBSOCKET_CLOSED code={code} reason={reason} disconnect_count={self.disconnect_count}")

    def _on_error(self, ws, code, reason):
        logger.error(f"WEBSOCKET_ERROR code={code} reason={reason}")

    def _on_reconnect(self, ws, attempts_count):
        logger.warning(f"WEBSOCKET_RECONNECTING attempt={attempts_count}")

    def _on_noreconnect(self, ws):
        self._connected.clear()
        logger.error("WEBSOCKET_RECONNECT_GIVEN_UP -- exhausted automatic reconnect attempts")

    def _on_ticks(self, ws, ticks):
        for raw in ticks:
            normalized = normalize_kite_tick(raw, clock_fn=self._clock_fn)
            if normalized is not None:
                self.tick_store.update(normalized)
