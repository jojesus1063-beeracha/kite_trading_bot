"""
PAPER-mode broker adapter (spec #23): simulates fills using live
bid/ask from the tick store plus configurable slippage, while
implementing the same subset of the KiteConnect interface that
execution/entry.py and execution/exit.py already use.

This is the entire point of this module: PAPER and LIVE run through
IDENTICAL entry/exit code. Only the object passed in as `kite`
differs. That means everything already tested against a real
KiteConnect-shaped mock in test_entry.py/test_exit.py is exercised
the same way in PAPER mode -- there is no separate, less-tested PAPER
code path to diverge from LIVE and hide a bug in.

V1 SIMPLIFICATION (documented, not hidden): every simulated order
fills its FULL requested quantity immediately, capped at the
submitted limit price (never simulates a fill worse than the limit
allows, matching real limit-order semantics, but also never simulates
a partial fill or a non-fill). Real markets can do both, especially
at the open. Treat PAPER results as an optimistic upper bound on fill
quality relative to what LIVE would actually achieve -- this is an
open item for a later phase, not a hidden gap.

NEVER places a real order. positions() always returns empty net
positions -- paper trades never appear on the real broker's books, so
crash_recovery's broker-reconciliation logic is deliberately bypassed
for PAPER mode at the launcher level (see launcher.py).
"""
import logging
import uuid

from typing import Optional

logger = logging.getLogger("fno.paper_broker")


def simulate_buy_fill_price(limit_price: float, best_ask: float, slippage_pct: float) -> float:
    """Never worse (higher) than the submitted limit -- a real limit
    BUY can't fill above its limit price. `best_ask * (1 + slippage/100)`
    models the fill drifting worse than the visible best ask as it eats
    through the book; capping at limit_price keeps this consistent with
    real order semantics."""
    if best_ask is None or best_ask <= 0:
        return limit_price
    return min(limit_price, best_ask * (1 + slippage_pct / 100))


def simulate_sell_fill_price(limit_price: float, best_bid: float, slippage_pct: float) -> float:
    """Never worse (lower) than the submitted limit -- mirror of the
    buy case for exits."""
    if best_bid is None or best_bid <= 0:
        return limit_price
    return max(limit_price, best_bid * (1 - slippage_pct / 100))


class PaperBroker:
    """
    Wraps a real (authenticated) KiteConnect client for read-only data
    (instruments, margins) while intercepting order placement/
    modification/cancellation/history to simulate against live tick
    data instead. `register_instrument_token` must be called for every
    tradingsymbol this bot might trade, before any order for it is
    submitted, so simulated fills can be priced off the right tick.
    """

    def __init__(self, real_kite, tick_store, cfg, clock_fn=None):
        self._real_kite = real_kite
        self._tick_store = tick_store
        self._cfg = cfg
        self._token_by_symbol: dict[str, int] = {}
        self._orders: dict[str, dict] = {}
        self._next_id = 1

        self.TRANSACTION_TYPE_BUY = getattr(real_kite, "TRANSACTION_TYPE_BUY", "BUY")
        self.TRANSACTION_TYPE_SELL = getattr(real_kite, "TRANSACTION_TYPE_SELL", "SELL")

    def register_instrument_token(self, tradingsymbol: str, instrument_token: int):
        self._token_by_symbol[tradingsymbol] = instrument_token

    def place_order(self, *, variety, exchange, tradingsymbol, transaction_type, quantity,
                     product, order_type, price, market_protection=None, **kwargs):
        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
  
        token = self._token_by_symbol.get(tradingsymbol)
        tick = self._tick_store.latest(token) if token is not None else None
        slippage_pct = getattr(self._cfg, "PAPER_SLIPPAGE_PCT", 0.5)

        if transaction_type == self.TRANSACTION_TYPE_BUY:
            best_ask = tick.best_ask if tick else None
            fill_price = simulate_buy_fill_price(price, best_ask, slippage_pct)
        else:
            best_bid = tick.best_bid if tick else None
            fill_price = simulate_sell_fill_price(price, best_bid, slippage_pct)

        self._orders[order_id] = {
            "status": "COMPLETE", "filled_quantity": quantity, "pending_quantity": 0,
            "cancelled_quantity": 0, "average_price": fill_price,
            "status_message": "PAPER_SIMULATED_FILL", "exchange_order_id": order_id,
        }
        logger.info(f"PAPER FILL {tradingsymbol} {transaction_type} qty={quantity} "
                    f"limit={price} simulated_fill={fill_price:.2f}")
        return order_id

    def modify_order(self, *, variety, order_id, price=None, **kwargs):
        """No-op beyond bookkeeping: this simplified model fills
        immediately on submission, so a reprice on an already-COMPLETE
        paper order has nothing left to affect -- present for interface
        compatibility with the real escalation ladder in exit.py."""
        order = self._orders.get(order_id)
        if order is not None and price is not None:
            order["last_modify_price"] = price
        return order_id

    def cancel_order(self, *, variety, order_id, **kwargs):
        order = self._orders.get(order_id)
        if order is not None and order["filled_quantity"] == 0:
            order["status"] = "CANCELLED"
        return order_id

    def order_history(self, order_id):
        order = self._orders.get(order_id)
        if order is None:
            return []
        return [dict(order)]

    def positions(self):
        """Always empty -- paper trades never touch the real broker's
        books. Callers (crash_recovery) must not use this to reconcile
        PAPER-mode local state; local position_store IS the source of
        truth for PAPER, by design (see launcher.py)."""
        return {"net": [], "day": []}

    def __getattr__(self, name):
        """Delegates anything not explicitly overridden (instruments(),
        margins(), order_margins(), etc.) to the real, read-only-safe
        broker client."""
        return getattr(self._real_kite, name)
