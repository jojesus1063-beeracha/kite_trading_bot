"""
Aggressive-limit entry execution with bounded retries (spec #7-9).

Pure decision logic (price/slippage/precondition math) is separated
from the broker-submission orchestration, so the interesting behavior
(when to abort, when to retry, when slippage is too much) is testable
without a real kite/WebSocket connection at all.

Never assumes order price = execution price (spec #9): every attempt
goes through order_store + order_verification, exactly like the
equity bot's mature executor.py pattern, and only a broker-confirmed
fill is ever reported as success.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

from fno_bot.execution import order_store
from fno_bot.execution.order_verification import verify_order_execution

logger = logging.getLogger("fno.entry")


# --- pure decision logic (no I/O) -----------------------------------

def compute_entry_limit_price(reference_price: float, buffer_pct: float) -> float:
    """Aggressive limit = reference * (1 + buffer/100). `reference_price`
    should be the freshest available price (best ask on a retry, first
    tick on attempt 1) -- never a stale value from a prior attempt."""
    if reference_price <= 0:
        raise ValueError(f"reference_price must be positive, got {reference_price}")
    return reference_price * (1 + buffer_pct / 100)


def slippage_pct(original_reference_price: float, candidate_price: float) -> float:
    """% by which candidate_price exceeds the ORIGINAL first-tick
    reference (not the previous attempt's price) -- slippage is always
    measured against the original decision point, so it accumulates
    correctly across multiple retries instead of resetting each time."""
    if original_reference_price <= 0:
        return float("inf")
    return (candidate_price - original_reference_price) / original_reference_price * 100


@dataclass(frozen=True)
class EntryPrecheck:
    ok: bool
    reason: Optional[str]
    limit_price: Optional[float]


def check_entry_preconditions(
    *,
    original_reference_price: float,
    current_best_ask: Optional[float],
    tick_age_ms: Optional[float],
    spread_pct: Optional[float],
    max_tick_age_ms: float,
    max_spread_pct: float,
    max_entry_slippage_pct: float,
    entry_buffer_pct: float,
    signal_still_valid: bool,
) -> EntryPrecheck:
    """
    Evaluated fresh before EVERY attempt (spec #8: "each retry must use
    fresh market depth ... never blindly resend the same stale limit
    price"). Any single failing condition aborts this attempt --
    callers decide whether that means retry-later or ABORT ENTRY
    entirely (see execute_entry).
    """
    if not signal_still_valid:
        return EntryPrecheck(False, "SIGNAL_NO_LONGER_VALID", None)
    if current_best_ask is None:
        return EntryPrecheck(False, "NO_ASK_AVAILABLE", None)
    if tick_age_ms is None or tick_age_ms > max_tick_age_ms:
        return EntryPrecheck(False, f"STALE_TICK (age_ms={tick_age_ms})", None)
    if spread_pct is None or spread_pct > max_spread_pct:
        return EntryPrecheck(False, f"SPREAD_TOO_WIDE (spread_pct={spread_pct})", None)

    limit_price = compute_entry_limit_price(current_best_ask, entry_buffer_pct)
    implied_slippage = slippage_pct(original_reference_price, limit_price)
    if implied_slippage > max_entry_slippage_pct:
        return EntryPrecheck(
            False,
            f"MAX_SLIPPAGE_EXCEEDED (implied_slippage_pct={implied_slippage:.2f} "
            f"> max={max_entry_slippage_pct})",
            None,
        )
    return EntryPrecheck(True, None, limit_price)


# --- broker orchestration --------------------------------------------

@dataclass(frozen=True)
class EntryResult:
    success: bool
    status: str          # FILLED | PARTIALLY_FILLED | ABORTED | NO_FILL
    filled_quantity: int
    average_price: Optional[float]
    attempts_made: int
    abort_reason: Optional[str]
    order_id: Optional[str]
    operation_id: Optional[str]


def _cancel_stale_attempt(kite, order_id: str, operation_id: str, cfg):
    """
    Called before a retry when the PREVIOUS attempt's order is still
    live but unfilled (non-terminal TIMEOUT) -- cancels it on the
    broker and marks its intent resolved, so the next attempt's
    create_order_intent() isn't blocked by duplicate-order protection
    (which is correctly guarding against exactly this: a second live
    order for the same symbol+action while the first is still
    outstanding). Never blindly resends a stale price (spec #8) --
    this is what makes a genuinely FRESH retry possible instead of
    just leaving the old one to rot.
    """
    try:
        kite.cancel_order(variety=cfg.VARIETY, order_id=order_id)
    except Exception as e:
        logger.warning(f"cancel_order failed for stale entry attempt order_id={order_id}: {e} "
                        f"-- it may still fill unexpectedly; next attempt's duplicate-guard will catch that")
    finally:
        try:
            order_store.mark_order_resolved(operation_id, resolution_reason="CANCELLED_FOR_RETRY")
        except order_store.OrderStoreError:
            pass  # already resolved or otherwise inconsistent -- don't let cleanup itself raise


def _submit_one_attempt(kite, symbol, exchange, quantity, limit_price, cfg):
    """One ENTRY order submission + verification, using the same
    durable intent -> submit -> attach -> verify -> resolve lifecycle
    as the equity bot's executor.place_entry_order(). Returns a dict
    mirroring that function's return shape."""

    def _rejected(reason, operation_id=None):
        return {"success": False, "order_id": None, "operation_id": operation_id,
                "filled_quantity": 0, "average_price": None, "reason": reason}

    if order_store.has_unresolved_order(symbol, exchange, "ENTRY"):
        return _rejected("ENTRY_BLOCKED_PENDING_ORDER")

    try:
        operation_id = order_store.create_order_intent(symbol, exchange, "ENTRY", "BUY", quantity)
    except order_store.UnresolvedOrderExistsError:
        return _rejected("ENTRY_BLOCKED_PENDING_ORDER")
    except order_store.OrderStoreError as e:
        return _rejected(f"intent creation failed: {e}")

    try:
        order_id = kite.place_order(
            variety=cfg.VARIETY, exchange=exchange, tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY, quantity=quantity,
            product=cfg.PRODUCT, order_type=cfg.ORDER_TYPE_ENTRY, price=round(limit_price, 2),
            market_protection=cfg.MARKET_PROTECTION,
        )
    except Exception as e:
        logger.error(f"CRITICAL: entry submission uncertain for {symbol} (operation_id={operation_id}): {e}")
        return {"success": False, "order_id": None, "operation_id": operation_id,
                "filled_quantity": 0, "average_price": None,
                "reason": f"submission exception, broker outcome unknown: {e}"}

    order_store.attach_broker_order_id(operation_id, order_id)

    exec_result = verify_order_execution(
        kite, order_id, quantity,
        max_wait_seconds=getattr(cfg, "ORDER_VERIFY_MAX_WAIT_SECONDS", 8),
        poll_interval_seconds=getattr(cfg, "ORDER_VERIFY_POLL_INTERVAL_SECONDS", 0.5),
    )
    order_store.update_order_verification(operation_id, exec_result)
    if exec_result.terminal:
        order_store.mark_order_resolved(operation_id, resolution_reason=exec_result.status)

    return {
        "success": exec_result.filled_quantity > 0,
        "order_id": order_id, "operation_id": operation_id,
        "filled_quantity": exec_result.filled_quantity,
        "average_price": exec_result.average_price,
        "reason": exec_result.status,
    }


def execute_entry(
    kite,
    *,
    symbol: str,
    exchange: str,
    quantity: int,
    original_reference_price: float,
    cfg,
    fetch_fresh_market: Callable[[], dict],
    signal_still_valid_fn: Callable[[], bool],
    audit_fn: Callable[..., None] = lambda *a, **k: None,
    sleep_fn=None,
) -> EntryResult:
    """
    Runs up to cfg.MAX_ENTRY_ATTEMPTS attempts. Before EVERY attempt
    (including the first), calls fetch_fresh_market() -- expected to
    return {"best_ask": float|None, "tick_age_ms": float|None,
    "spread_pct": float|None} read live from the tick store -- and
    signal_still_valid_fn() -- never reuses a previous attempt's
    market read (spec #8).

    Aborts (not "fails") when preconditions reject the attempt for a
    reason that won't improve with more attempts (signal invalidated,
    slippage ceiling breached) -- only stale-tick/wide-spread
    conditions are retried, since those can plausibly resolve within
    ENTRY_RETRY_BACKOFF_MS.
    """
    sleep_fn = sleep_fn or time.sleep
    max_attempts = getattr(cfg, "MAX_ENTRY_ATTEMPTS", 3)
    backoff_s = getattr(cfg, "ENTRY_RETRY_BACKOFF_MS", 250) / 1000

    attempts_made = 0
    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt
        market = fetch_fresh_market()
        signal_valid = signal_still_valid_fn()

        precheck = check_entry_preconditions(
            original_reference_price=original_reference_price,
            current_best_ask=market.get("best_ask"),
            tick_age_ms=market.get("tick_age_ms"),
            spread_pct=market.get("spread_pct"),
            max_tick_age_ms=cfg.MAX_TICK_AGE_MS,
            max_spread_pct=cfg.MAX_SPREAD_PCT,
            max_entry_slippage_pct=cfg.MAX_ENTRY_SLIPPAGE_PCT,
            entry_buffer_pct=cfg.ENTRY_BUFFER_PCT,
            signal_still_valid=signal_valid,
        )
        audit_fn("ENTRY_ATTEMPT", attempt=attempt, symbol=symbol, precheck_ok=precheck.ok,
                  reason=precheck.reason, limit_price=precheck.limit_price)

        if not precheck.ok:
            if precheck.reason in ("SIGNAL_NO_LONGER_VALID",) or (
                precheck.reason and precheck.reason.startswith("MAX_SLIPPAGE_EXCEEDED")
            ):
                logger.warning(f"ABORT ENTRY for {symbol}: {precheck.reason}")
                audit_fn("ENTRY_ABORTED", symbol=symbol, reason=precheck.reason, attempt=attempt)
                return EntryResult(False, "ABORTED", 0, None, attempts_made, precheck.reason, None, None)
            # stale tick / wide spread: worth one more attempt after backoff, if attempts remain
            if attempt < max_attempts:
                sleep_fn(backoff_s)
                continue
            audit_fn("ENTRY_ABORTED", symbol=symbol, reason=precheck.reason, attempt=attempt)
            return EntryResult(False, "ABORTED", 0, None, attempts_made, precheck.reason, None, None)

        submitted = _submit_one_attempt(kite, symbol, exchange, quantity, precheck.limit_price, cfg)
        audit_fn("ENTRY_SUBMITTED", symbol=symbol, attempt=attempt, limit_price=precheck.limit_price,
                  order_id=submitted.get("order_id"))

        filled = submitted["filled_quantity"]
        if filled >= quantity:
            audit_fn("ENTRY_FILLED", symbol=symbol, filled_quantity=filled,
                      average_price=submitted["average_price"], attempts=attempts_made)
            return EntryResult(True, "FILLED", filled, submitted["average_price"], attempts_made,
                                None, submitted["order_id"], submitted["operation_id"])
        if 0 < filled < quantity:
            audit_fn("ENTRY_PARTIAL", symbol=symbol, filled_quantity=filled,
                      requested_quantity=quantity, attempts=attempts_made)
            return EntryResult(True, "PARTIALLY_FILLED", filled, submitted["average_price"], attempts_made,
                                None, submitted["order_id"], submitted["operation_id"])

        # No fill this attempt. Cancel the dangling unfilled order regardless of
        # whether we're about to retry or give up entirely -- an uncancelled
        # limit order left on the exchange after we've moved on (or stopped
        # trying) is a live ghost-fill risk, not a harmless no-op.
        if submitted.get("order_id") and submitted.get("operation_id"):
            _cancel_stale_attempt(kite, submitted["order_id"], submitted["operation_id"], cfg)
        if attempt < max_attempts:
            sleep_fn(backoff_s)

    audit_fn("ENTRY_ABORTED", symbol=symbol, reason="MAX_ATTEMPTS_EXHAUSTED_NO_FILL", attempt=attempts_made)
    return EntryResult(False, "NO_FILL", 0, None, attempts_made, "MAX_ATTEMPTS_EXHAUSTED_NO_FILL", None, None)
