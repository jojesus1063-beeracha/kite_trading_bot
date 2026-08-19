"""
In-memory cache of the latest tick per instrument token, with
age-tracking so stale-data protection (spec #25) can be enforced
before every trading decision.

Deliberately storage-only: no broker/WebSocket code here, so it's
directly unit-testable with synthetic tick dicts (see
tests/test_tick_store.py) and directly reusable by replay mode
(spec #35), which feeds recorded ticks through this exact same store.
"""
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("fno.tick_store")


@dataclass(frozen=True)
class NormalizedTick:
    instrument_token: int
    last_price: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_bid_qty: Optional[int]
    best_ask_qty: Optional[int]
    volume: Optional[int]
    exchange_timestamp: Optional[str]   # broker-reported timestamp, if present, as ISO string
    received_monotonic: float           # time.monotonic() when this process received the tick


def normalize_kite_tick(raw: dict, clock_fn=None) -> Optional[NormalizedTick]:
    """
    Converts one raw KiteTicker tick dict (mode="full" preferred, for
    depth) into a NormalizedTick. Returns None for a tick missing the
    bare minimum (instrument_token, last_price) rather than raising --
    a single malformed tick must never crash the monitoring loop.
    """
    clock_fn = clock_fn or time.monotonic
    try:
        token = int(raw["instrument_token"])
        last_price = float(raw["last_price"])
    except (KeyError, TypeError, ValueError):
        logger.warning(f"Dropping malformed tick, missing/invalid token or last_price: {raw!r}")
        return None

    depth = raw.get("depth") or {}
    buy_levels = depth.get("buy") or []
    sell_levels = depth.get("sell") or []
    best_bid = float(buy_levels[0]["price"]) if buy_levels and buy_levels[0].get("price") else None
    best_ask = float(sell_levels[0]["price"]) if sell_levels and sell_levels[0].get("price") else None
    best_bid_qty = int(buy_levels[0]["quantity"]) if buy_levels and buy_levels[0].get("quantity") is not None else None
    best_ask_qty = int(sell_levels[0]["quantity"]) if sell_levels and sell_levels[0].get("quantity") is not None else None

    exch_ts = raw.get("last_trade_time") or raw.get("exchange_timestamp")
    exch_ts_str = exch_ts.isoformat() if hasattr(exch_ts, "isoformat") else (str(exch_ts) if exch_ts else None)

    return NormalizedTick(
        instrument_token=token,
        last_price=last_price,
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_qty=best_bid_qty,
        best_ask_qty=best_ask_qty,
        volume=raw.get("volume_traded") or raw.get("volume"),
        exchange_timestamp=exch_ts_str,
        received_monotonic=clock_fn(),
    )


class TickStore:
    """Not thread-safe by external contract -- callers (ticker.py's
    on_ticks callback) are expected to run on a single thread, matching
    KiteTicker's own threaded-callback model. Reads and writes are both
    simple dict operations, cheap enough not to need a lock for this
    single-writer/single-reader-thread-pair use."""

    def __init__(self, clock_fn=None):
        self._clock_fn = clock_fn or time.monotonic
        self._latest: dict[int, NormalizedTick] = {}

    def update(self, tick: NormalizedTick):
        self._latest[tick.instrument_token] = tick

    def latest(self, instrument_token: int) -> Optional[NormalizedTick]:
        return self._latest.get(instrument_token)

    def has_tick(self, instrument_token: int) -> bool:
        return instrument_token in self._latest

    def tick_age_ms(self, instrument_token: int, now: float = None) -> Optional[float]:
        tick = self.latest(instrument_token)
        if tick is None:
            return None
        now = now if now is not None else self._clock_fn()
        return (now - tick.received_monotonic) * 1000

    def is_fresh(self, instrument_token: int, max_age_ms: float, now: float = None) -> bool:
        """False (never trade) if there's no tick at all yet, OR the
        latest tick is older than max_age_ms -- fails closed, not open."""
        age = self.tick_age_ms(instrument_token, now)
        if age is None:
            return False
        return age <= max_age_ms

    def spread_pct(self, instrument_token: int) -> Optional[float]:
        tick = self.latest(instrument_token)
        if tick is None or tick.best_bid is None or tick.best_ask is None or tick.best_bid <= 0:
            return None
        mid = (tick.best_bid + tick.best_ask) / 2
        if mid <= 0:
            return None
        return (tick.best_ask - tick.best_bid) / mid * 100
