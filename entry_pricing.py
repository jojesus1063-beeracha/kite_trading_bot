"""
WS-based entry pricing (Phase 4 of the WS candle engine).

Pure helper functions only -- this module does NOT call
executor.place_entry_order() or kite.place_order() itself, and does
NOT change cfg.ORDER_TYPE_ENTRY's default of "MARKET" for anyone.

Wiring this into executor.py is a deliberate, separate decision: it
means switching entry orders from MARKET to a marketable LIMIT order,
which changes your fill guarantee (a MARKET order always fills, a
capped LIMIT order can NOT fill if price moves past the cap before
execution). That trade-off (guaranteed fill vs slippage protection) is
yours to make explicitly, not something to flip on as a side effect of
adding WS support. See INTEGRATION.md for exactly where this plugs in
if/when you decide to.

Everything here operates on a `depth` dict shaped like Kite's own
full-mode tick depth: {"buy": [{"price":..., "quantity":..., "orders":...}, ...],
"sell": [...]}, five levels each, as delivered by ws_ticker.py's
MODE_FULL subscription.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("entry_pricing")


@dataclass
class PricingResult:
    approved: bool
    reason: Optional[str]
    limit_price: Optional[float] = None
    best_price: Optional[float] = None   # best ask (BUY) / best bid (SELL) used
    spread_pct: Optional[float] = None
    depth_at_level: Optional[int] = None  # total quantity available at the best level


def check_tick_freshness(gap_tracker, symbol: str, max_age_seconds: float, now: Optional[float] = None) -> Optional[str]:
    """
    Returns None if the tick is fresh enough, else a rejection reason
    string. `gap_tracker` is a ws_ticker.GapTracker instance.
    """
    age = gap_tracker.seconds_since_last_tick(symbol, now)
    if age is None:
        return "no WS tick received yet for this symbol"
    if age > max_age_seconds:
        return f"latest tick is {age:.1f}s old (max allowed {max_age_seconds}s)"
    return None


def best_price_and_depth(depth: dict, direction: str) -> tuple:
    """
    direction: "BUY" or "SELL".
    BUY needs the best ASK (what you'd pay to buy).
    SELL needs the best BID (what you'd receive to sell).
    Returns (best_price, quantity_at_best_level) or (None, None) if
    that side of the book is empty.
    """
    side = "sell" if direction == "BUY" else "buy"
    levels = depth.get(side) if depth else None
    if not levels:
        return None, None
    best = levels[0]
    return best.get("price"), best.get("quantity")


def compute_spread_pct(depth: dict) -> Optional[float]:
    """Bid-ask spread as a percentage of the mid price, or None if either side is empty."""
    if not depth:
        return None
    buy_levels, sell_levels = depth.get("buy"), depth.get("sell")
    if not buy_levels or not sell_levels:
        return None
    best_bid = buy_levels[0].get("price")
    best_ask = sell_levels[0].get("price")
    if not best_bid or not best_ask:
        return None
    mid = (best_bid + best_ask) / 2
    if mid == 0:
        return None
    return (best_ask - best_bid) / mid * 100


def compute_marketable_limit_price(direction: str, best_price: float, max_slippage_pct: float) -> float:
    """
    A marketable limit order priced to nearly-always fill immediately
    against the current best level, while capping the worst acceptable
    price. BUY caps upside (won't chase above cap); SELL caps downside.
    """
    if direction == "BUY":
        return round(best_price * (1 + max_slippage_pct / 100), 2)
    return round(best_price * (1 - max_slippage_pct / 100), 2)


def evaluate_entry_price(
    symbol: str,
    direction: str,
    signal_price: float,
    depth: dict,
    gap_tracker,
    cfg,
    now: Optional[float] = None,
) -> PricingResult:
    """
    Full Phase-4 pre-submission check, in the order specified by the
    original architecture doc:
      1. reject if the latest tick is stale
      2. read best ask (BUY) / best bid (SELL)
      3. check spread and available depth
      4. re-verify adverse-move / absolute-drift limits against this fresh price

    UPDATE: a separate change (PR #8, "Reduce candle-close entry latency")
    landed in this repo after this module was first written, and it DOES
    now implement adverse-move/drift limits -- as module-level constants
    MAX_ADVERSE_LIVE_SLIPPAGE_PCT=0.15 and MAX_ABSOLUTE_SIGNAL_DRIFT_PCT=0.35
    in entry_quality.py, enforced against REST quotes (kite.quote()) as
    part of the ranked-candidate-scan pipeline, tested by
    test_fresh_entry_price.py. That is a DIFFERENT, already-wired,
    already-tested system from this one.

    This function's cfg.MAX_ADVERSE_MOVE_PCT / cfg.MAX_ABSOLUTE_DRIFT_PCT
    (via getattr, so it's a no-op unless you add them to config.py
    yourself) are NOT the same mechanism and are NOT currently connected
    to entry_quality.py's checks in any way. Before ever wiring this WS
    Phase-4 module into executor.py, decide deliberately whether you want
    a second, WS-depth-based fresh-price check running alongside
    entry_quality.py's REST-based one, or whether entry_quality.py's
    existing mechanism should just be fed WS-sourced prices instead of
    this module existing in parallel. Do not assume these can just
    coexist without a decision -- redundant risk checks are not free
    (they can produce two different, disagreeing verdicts on the same
    trade).
      5. return a capped marketable-limit price rather than a raw market order

    Returns a PricingResult; callers must check `.approved` before using
    `.limit_price`. This function places no orders and mutates nothing.
    """
    max_age = getattr(cfg, "WS_ENTRY_TICK_MAX_AGE_SECONDS", 2.0)
    staleness_reason = check_tick_freshness(gap_tracker, symbol, max_age, now)
    if staleness_reason:
        return PricingResult(approved=False, reason=staleness_reason)

    best_price, depth_qty = best_price_and_depth(depth, direction)
    if best_price is None:
        return PricingResult(approved=False, reason=f"no {'ask' if direction == 'BUY' else 'bid'} depth available")

    spread_pct = compute_spread_pct(depth)
    max_spread_pct = getattr(cfg, "WS_MAX_SPREAD_PCT", None)
    if max_spread_pct is not None and spread_pct is not None and spread_pct > max_spread_pct:
        return PricingResult(approved=False, reason=f"spread {spread_pct:.3f}% exceeds max {max_spread_pct}%",
                              best_price=best_price, spread_pct=spread_pct, depth_at_level=depth_qty)

    adverse_pct = getattr(cfg, "MAX_ADVERSE_MOVE_PCT", None)
    if adverse_pct is not None and signal_price:
        move_pct = abs(best_price - signal_price) / signal_price * 100
        adverse = (direction == "BUY" and best_price > signal_price) or (direction == "SELL" and best_price < signal_price)
        if adverse and move_pct > adverse_pct:
            return PricingResult(approved=False,
                                  reason=f"adverse move {move_pct:.3f}% since signal exceeds {adverse_pct}%",
                                  best_price=best_price, spread_pct=spread_pct, depth_at_level=depth_qty)

    absolute_drift_pct = getattr(cfg, "MAX_ABSOLUTE_DRIFT_PCT", None)
    if absolute_drift_pct is not None and signal_price:
        drift_pct = abs(best_price - signal_price) / signal_price * 100
        if drift_pct > absolute_drift_pct:
            return PricingResult(approved=False,
                                  reason=f"absolute drift {drift_pct:.3f}% since signal exceeds {absolute_drift_pct}%",
                                  best_price=best_price, spread_pct=spread_pct, depth_at_level=depth_qty)

    max_slippage_pct = getattr(cfg, "WS_MAX_SLIPPAGE_PCT", 0.15)
    limit_price = compute_marketable_limit_price(direction, best_price, max_slippage_pct)

    return PricingResult(approved=True, reason=None, limit_price=limit_price,
                          best_price=best_price, spread_pct=spread_pct, depth_at_level=depth_qty)
