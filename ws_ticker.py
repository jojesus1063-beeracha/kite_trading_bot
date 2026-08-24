"""
WebSocket tick ingestion (Phase 1 of the WS candle engine).

Wraps kiteconnect's KiteTicker to subscribe to the full watchlist +
sector indices and expose a simple, thread-safe per-symbol tick buffer.
This module ONLY ingests and buffers ticks -- it builds nothing and
places no orders. candle_engine.py (phase 2) consumes the buffer.

Enabling this is controlled by cfg.ENABLE_WS_CANDLES; when False (the
default), main.py never imports or starts this module, so today's
live behavior is completely unaffected. See config.py for the
WS_CANDLE_MODE ("shadow"/"live") flag that governs whether the rest
of the pipeline (candle_engine, indicators_incremental, entry_pricing)
is allowed to influence real orders once this is wired in.

Reconnect handling: KiteTicker's own reconnection (websocket-level) is
enabled via reconnect=True below. On top of that, this module tracks
the last-seen tick time per symbol so candle_engine can detect a gap
and backfill via the existing REST fetch_candles() path before trusting
tick-built candles again -- see GapTracker.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("ws_ticker")

# How many raw ticks to retain per symbol in memory. At NSE's busiest
# this comfortably covers several minutes even for high-volume names;
# candle_engine only ever needs the ticks since the last candle
# boundary, so this is a safety margin, not the working window.
TICK_BUFFER_MAXLEN = 2000

# A symbol with no tick for longer than this is considered "stale" for
# gap-detection purposes. Kept generous (2x the entry-pricing 2s
# staleness check) since illiquid names can legitimately go quiet for
# a few seconds without anything being wrong.
STALE_TICK_SECONDS = 5.0


class GapTracker:
    """
    Tracks per-symbol last-tick-time and exposes whether a reconnect or
    stall has left a gap that candle_engine should backfill via REST
    before trusting streamed candles for that symbol again.
    """

    def __init__(self):
        self._last_tick_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_disconnect_at: Optional[float] = None
        self.last_reconnect_at: Optional[float] = None

    def mark_tick(self, symbol: str, now: Optional[float] = None):
        now = now if now is not None else time.time()
        with self._lock:
            self._last_tick_at[symbol] = now

    def mark_disconnect(self):
        self.last_disconnect_at = time.time()
        logger.warning("ws_ticker: disconnected")

    def mark_reconnect(self):
        self.last_reconnect_at = time.time()
        logger.warning("ws_ticker: reconnected -- caller should backfill any gap via REST")

    def is_stale(self, symbol: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            last = self._last_tick_at.get(symbol)
        if last is None:
            return True
        return (now - last) > STALE_TICK_SECONDS

    def seconds_since_last_tick(self, symbol: str, now: Optional[float] = None) -> Optional[float]:
        now = now if now is not None else time.time()
        with self._lock:
            last = self._last_tick_at.get(symbol)
        if last is None:
            return None
        return now - last


class TickBuffer:
    """
    Thread-safe ring buffer of raw ticks per symbol. Stores the minimal
    fields candle_engine needs: exchange timestamp, last_traded_price,
    and cumulative volume (so candle_engine can diff it for per-tick
    volume delta, matching Kite's own OHLCV semantics).
    """

    def __init__(self, maxlen: int = TICK_BUFFER_MAXLEN):
        self._buffers: dict[str, deque] = {}
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def append(self, symbol: str, tick: dict):
        with self._lock:
            buf = self._buffers.setdefault(symbol, deque(maxlen=self._maxlen))
            buf.append(tick)

    def ticks_since(self, symbol: str, since: datetime) -> list[dict]:
        """Returns buffered ticks for `symbol` with exchange_timestamp >= since, oldest first."""
        with self._lock:
            buf = self._buffers.get(symbol)
            if not buf:
                return []
            return [t for t in buf if t["exchange_timestamp"] >= since]

    def latest(self, symbol: str) -> Optional[dict]:
        with self._lock:
            buf = self._buffers.get(symbol)
            if not buf:
                return None
            return buf[-1]

    def ticks_received_since(self, symbol: str, received_at: float) -> list[dict]:
        """Return buffered ticks received at/after a wall-clock epoch cutoff."""
        with self._lock:
            buf = self._buffers.get(symbol)
            if not buf:
                return []
            return [
                tick for tick in buf
                if float(tick.get("received_at") or 0.0) >= received_at
            ]


class WSTicker:
    """
    Thin wrapper around kiteconnect.KiteTicker. Subscribes to every
    instrument token in `token_to_symbol` (built by the caller from the
    watchlist + sector indices, same instrument-token lookup as
    data_feed.get_instrument_token()) and feeds every tick into a
    TickBuffer plus a GapTracker.

    This class never touches strategy, risk, or order-placement code --
    it is the same "entirely independent" separation of concerns used
    by scheduler.py.
    """

    def __init__(self, api_key: str, access_token: str, token_to_symbol: dict[int, str],
                 on_tick: Optional[Callable[[str, dict], None]] = None):
        # Imported lazily so environments that never enable
        # ENABLE_WS_CANDLES don't need the extra import path exercised
        # at module load time.
        from kiteconnect import KiteTicker

        self.token_to_symbol = token_to_symbol
        self.tick_buffer = TickBuffer()
        self.gap_tracker = GapTracker()
        self._on_tick = on_tick
        self._connected = threading.Event()

        self.kws = KiteTicker(api_key, access_token, reconnect=True,
                               reconnect_max_delay=30, reconnect_max_tries=300)
        self.kws.on_ticks = self._handle_ticks
        self.kws.on_connect = self._handle_connect
        self.kws.on_close = self._handle_close
        self.kws.on_error = self._handle_error
        self.kws.on_reconnect = self._handle_reconnect
        self.kws.on_noreconnect = self._handle_noreconnect

    # -- KiteTicker callbacks -------------------------------------------------

    def _handle_connect(self, ws, response):
        tokens = list(self.token_to_symbol.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)  # full mode for bid/ask depth (phase 4 entry pricing)
        self._connected.set()
        logger.info(f"ws_ticker: connected, subscribed to {len(tokens)} instruments (MODE_FULL)")

    def _handle_ticks(self, ws, ticks):
        now = time.time()
        for raw in ticks:
            token = raw.get("instrument_token")
            symbol = self.token_to_symbol.get(token)
            if symbol is None:
                continue  # tick for an instrument we didn't ask for -- ignore

            ts = raw.get("exchange_timestamp") or raw.get("last_trade_time")
            if ts is None:
                continue
            # IMPORTANT: do NOT assume a timezone here. Kite's ticker
            # returns datetimes in whatever convention the connected
            # KiteTicker instance uses (observed as naive IST, matching
            # historical_data()'s convention before data_feed.py's own
            # normalization) -- NOT UTC. Forcing UTC on a naive
            # timestamp would silently misalign every comparison against
            # REST candle dates. Pass the timestamp through exactly as
            # Kite provides it; candle_engine/ws_integration normalize
            # against the REST DataFrame's actual tz at the point of
            # comparison, with a fail-safe that skips using WS data
            # entirely (falling back to REST) if normalization fails.

            tick = {
                "exchange_timestamp": ts,
                "last_price": raw.get("last_price"),
                "volume_traded": raw.get("volume_traded"),  # cumulative for the session
                "depth": raw.get("depth"),  # bid/ask levels, used by phase-4 entry pricing
                "received_at": now,
            }
            self.tick_buffer.append(symbol, tick)
            self.gap_tracker.mark_tick(symbol, now)

            if self._on_tick is not None:
                try:
                    self._on_tick(symbol, tick)
                except Exception:
                    logger.exception(f"ws_ticker: on_tick callback failed for {symbol}")

    def _handle_close(self, ws, code, reason):
        self._connected.clear()
        self.gap_tracker.mark_disconnect()
        logger.warning(f"ws_ticker: closed (code={code}, reason={reason})")

    def _handle_error(self, ws, code, reason):
        logger.error(f"ws_ticker: error (code={code}, reason={reason})")

    def _handle_reconnect(self, ws, attempts_count):
        logger.warning(f"ws_ticker: reconnecting (attempt {attempts_count})")

    def _handle_noreconnect(self, ws):
        self._connected.clear()
        logger.critical("ws_ticker: gave up reconnecting -- caller must fall back to REST-only mode")

    # -- lifecycle --------------------------------------------------------

    def start(self, threaded: bool = True):
        self.kws.connect(threaded=threaded)

    def stop(self):
        try:
            self.kws.close()
        except Exception:
            logger.exception("ws_ticker: error during close()")

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def is_connected(self) -> bool:
        return self._connected.is_set()


def build_token_map(kite, watchlist: list[dict], sector_indices: Optional[list[dict]] = None) -> dict[int, str]:
    """
    Builds {instrument_token: symbol} for the watchlist plus optional
    sector indices, reusing data_feed.get_instrument_token() so token
    resolution is identical to the existing REST path -- no duplicate
    lookup logic to drift out of sync.
    """
    from data_feed import get_instrument_token

    token_to_symbol: dict[int, str] = {}
    entries = list(watchlist) + list(sector_indices or [])
    for entry in entries:
        symbol, exchange = entry["symbol"], entry["exchange"]
        try:
            token = get_instrument_token(kite, symbol, exchange)
            token_to_symbol[token] = symbol
        except Exception as e:
            logger.warning(f"ws_ticker: could not resolve token for {exchange}:{symbol} -- {e}")
    return token_to_symbol
