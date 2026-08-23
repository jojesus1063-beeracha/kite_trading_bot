"""
Exit escalation ladder (spec #16): LIMIT EXIT -> wait -> refresh depth
-> reprice (via kite.modify_order on the SAME order, so partial fills
already confirmed on it are never touched) -> repeat within bounded
attempts -> emergency executable exit if the ladder is exhausted
without a full fill.

Priority when a stop-loss/emergency condition fires is getting out
safely, not getting the best price (spec #16) -- the emergency step
uses an aggressive-to-marketable price, never leaves a position open
indefinitely waiting for an ideal fill.

Handles partial fills explicitly (spec #17): every step re-derives
the REMAINING quantity from the broker-confirmed filled_quantity on
the existing order, never from a locally-assumed running total, and
never submits a NEW order for more than the remaining open quantity.
"""
import time
import logging
from dataclasses import dataclass
from typing import Optional, Callable

from fno_bot.execution import order_store
from fno_bot.execution.order_verification import verify_order_execution

logger = logging.getLogger("fno.exit")


def compute_exit_limit_price(best_bid: float, buffer_pct: float) -> float:
    """Slightly aggressive of the best bid, so the order is likely to
    cross and fill rather than just sitting at the top of the book."""
    if best_bid <= 0:
        raise ValueError(f"best_bid must be positive, got {best_bid}")
    return best_bid * (1 - buffer_pct / 100)


def compute_emergency_exit_price(best_bid: float, tick_size: float = 0.05) -> float:
    """
    A deliberately marketable price for the final escalation step:
    priced meaningfully below the current best bid so it crosses
    whatever is resting on the buy side immediately, favoring a
    confirmed exit over price quality (spec #16's stated priority).
    Floored at one tick above zero so it's never an invalid <=0 price.
    """
    aggressive = best_bid * 0.90  # 10% below current best bid
    return max(aggressive, tick_size)


@dataclass(frozen=True)
class ExitResult:
    success: bool
    status: str            # FILLED | PARTIALLY_FILLED | NO_FILL
    filled_quantity: int
    remaining_quantity: int
    average_price: Optional[float]
    escalation_steps: int
    order_id: Optional[str]
    operation_id: Optional[str]


def _submit_exit_order(kite, symbol, exchange, quantity, price, action, direction, cfg):
    """direction = ORIGINAL position direction ("BUY" for a long option);
    the exit reverses it (SELL). action = "EXIT" or "FORCE_EXIT"."""
    exit_side = "SELL" if direction == "BUY" else "BUY"

    if order_store.has_unresolved_order(symbol, exchange, action):
        return {"success": False, "order_id": None, "operation_id": None,
                "filled_quantity": 0, "average_price": None, "reason": f"{action}_BLOCKED_PENDING_ORDER"}

    try:
        operation_id = order_store.create_order_intent(symbol, exchange, action, exit_side, quantity)
    except order_store.UnresolvedOrderExistsError:
        return {"success": False, "order_id": None, "operation_id": None,
                "filled_quantity": 0, "average_price": None, "reason": f"{action}_BLOCKED_PENDING_ORDER"}

    transaction_type = kite.TRANSACTION_TYPE_SELL if exit_side == "SELL" else kite.TRANSACTION_TYPE_BUY
    try:
        order_id = kite.place_order(
            variety=cfg.VARIETY, exchange=exchange, tradingsymbol=symbol,
            transaction_type=transaction_type, quantity=quantity, product=cfg.PRODUCT,
            order_type="LIMIT", price=round(price, 2), market_protection=cfg.MARKET_PROTECTION,
        )
    except Exception as e:
        logger.error(f"CRITICAL: {action} submission uncertain for {symbol} (operation_id={operation_id}): {e}")
        return {"success": False, "order_id": None, "operation_id": operation_id,
                "filled_quantity": 0, "average_price": None,
                "reason": f"submission exception, broker outcome unknown: {e}"}

    order_store.attach_broker_order_id(operation_id, order_id)
    return {"success": True, "order_id": order_id, "operation_id": operation_id,
            "filled_quantity": 0, "average_price": None, "reason": None}


def execute_exit(
    kite,
    *,
    symbol: str,
    exchange: str,
    quantity: int,
    direction: str,               # original position direction, e.g. "BUY"
    action: str,                  # "EXIT" or "FORCE_EXIT"
    cfg,
    fetch_fresh_best_bid: Callable[[], Optional[float]],
    audit_fn: Callable[..., None] = lambda *a, **k: None,
    sleep_fn=None,
) -> ExitResult:
    """
    Submits the initial exit at a bid-relative aggressive limit, then
    escalates: wait -> refresh depth -> reprice (kite.modify_order on
    the SAME order_id, so already-confirmed partial fills are
    preserved) -> repeat up to cfg.MAX_EXIT_REPRICE_ATTEMPTS -> a final
    emergency marketable price if still not fully filled.

    Never submits a second NEW order for the same exit while the first
    is still open -- always modifies the existing one, exactly
    matching the ladder's "modify/reprice" step rather than risking
    two live sell orders for the same position (spec #17: never sell
    more than the actual open position).
    """
    sleep_fn = sleep_fn or time.sleep
    wait_s = getattr(cfg, "EXIT_REPRICE_WAIT_MS", 500) / 1000
    max_reprice = getattr(cfg, "MAX_EXIT_REPRICE_ATTEMPTS", 4)

    best_bid = fetch_fresh_best_bid()
    if best_bid is None or best_bid <= 0:
        # No usable bid at all -- go straight to a best-effort emergency
        # MARKET order rather than failing to exit entirely.
        best_bid = 0.05  # will be overridden by emergency pricing below if still unusable
    initial_price = compute_exit_limit_price(best_bid, getattr(cfg, "EXIT_ORDER_BUFFER_PCT", 1.0))

    submitted = _submit_exit_order(kite, symbol, exchange, quantity, initial_price, action, direction, cfg)
    audit_fn("EXIT_SUBMITTED", symbol=symbol, action=action, price=initial_price, order_id=submitted.get("order_id"))

    if not submitted["success"] or submitted["order_id"] is None:
        return ExitResult(False, "NO_FILL", 0, quantity, None, 0, submitted.get("order_id"), submitted.get("operation_id"))

    order_id = submitted["order_id"]
    operation_id = submitted["operation_id"]

    filled_quantity = 0
    average_price = None
    step = 0

    for step in range(1, max_reprice + 2):  # +1 extra pass = the emergency step
        sleep_fn(wait_s)
        exec_result = verify_order_execution(
            kite, order_id, quantity,
            max_wait_seconds=getattr(cfg, "ORDER_VERIFY_MAX_WAIT_SECONDS", 8),
            poll_interval_seconds=getattr(cfg, "ORDER_VERIFY_POLL_INTERVAL_SECONDS", 0.5),
        )
        order_store.update_order_verification(operation_id, exec_result)
        filled_quantity = exec_result.filled_quantity
        average_price = exec_result.average_price
        remaining = quantity - filled_quantity

        if filled_quantity >= quantity or exec_result.status in ("REJECTED", "CANCELLED") or exec_result.terminal:
            if exec_result.terminal:
                order_store.mark_order_resolved(operation_id, resolution_reason=exec_result.status)
            if filled_quantity >= quantity:
                audit_fn("EXIT_FILLED", symbol=symbol, filled_quantity=filled_quantity,
                          average_price=average_price, escalation_steps=step)
                return ExitResult(True, "FILLED", filled_quantity, 0, average_price, step, order_id, operation_id)
            if filled_quantity > 0:
                audit_fn("EXIT_PARTIAL", symbol=symbol, filled_quantity=filled_quantity,
                          remaining_quantity=remaining, escalation_steps=step)
            break  # terminal but not fully filled (e.g. cancelled/rejected with partial) -- stop the ladder here

        if step > max_reprice:
            break  # ladder exhausted, one more (emergency) pass happens below

        # Not yet filled, not terminal -- refresh depth and reprice the SAME order.
        fresh_bid = fetch_fresh_best_bid()
        if fresh_bid is None or fresh_bid <= 0:
            continue  # no usable fresh price this round; try again next iteration within the loop bound
        new_price = compute_exit_limit_price(fresh_bid, getattr(cfg, "EXIT_ORDER_BUFFER_PCT", 1.0))
        try:
            kite.modify_order(variety=cfg.VARIETY, order_id=order_id, price=round(new_price, 2))
            audit_fn("EXIT_REPRICE", symbol=symbol, order_id=order_id, new_price=new_price, step=step)
        except Exception as e:
            logger.warning(f"modify_order failed for {symbol} order_id={order_id} at step {step}: {e}")

    remaining = quantity - filled_quantity
    if remaining <= 0:
        return ExitResult(True, "FILLED", filled_quantity, 0, average_price, step, order_id, operation_id)

    # --- Emergency step: escalate to a marketable price for the remainder ---
    fresh_bid = fetch_fresh_best_bid() or best_bid
    emergency_price = compute_emergency_exit_price(fresh_bid)
    try:
        kite.modify_order(variety=cfg.VARIETY, order_id=order_id, price=round(emergency_price, 2))
        audit_fn("EXIT_EMERGENCY_REPRICE", symbol=symbol, order_id=order_id, price=emergency_price)
    except Exception as e:
        logger.error(f"CRITICAL: emergency reprice failed for {symbol} order_id={order_id}: {e} "
                     f"-- position may remain open, requires manual/next-cycle attention")

    exec_result = verify_order_execution(
        kite, order_id, quantity,
        max_wait_seconds=getattr(cfg, "ORDER_VERIFY_MAX_WAIT_SECONDS", 8),
        poll_interval_seconds=getattr(cfg, "ORDER_VERIFY_POLL_INTERVAL_SECONDS", 0.5),
    )
    order_store.update_order_verification(operation_id, exec_result)
    if exec_result.terminal:
        order_store.mark_order_resolved(operation_id, resolution_reason=exec_result.status)

    filled_quantity = exec_result.filled_quantity
    remaining = quantity - filled_quantity
    if filled_quantity >= quantity:
        audit_fn("EXIT_FILLED", symbol=symbol, filled_quantity=filled_quantity,
                  average_price=exec_result.average_price, escalation_steps=step + 1, emergency=True)
        return ExitResult(True, "FILLED", filled_quantity, 0, exec_result.average_price, step + 1, order_id, operation_id)
    if filled_quantity > 0:
        audit_fn("EXIT_PARTIAL", symbol=symbol, filled_quantity=filled_quantity,
                  remaining_quantity=remaining, escalation_steps=step + 1, emergency=True)
        return ExitResult(True, "PARTIALLY_FILLED", filled_quantity, remaining, exec_result.average_price,
                           step + 1, order_id, operation_id)

    logger.error(f"CRITICAL: exit ladder exhausted with ZERO fill for {symbol} order_id={order_id} "
                 f"-- position remains fully open, requires immediate manual attention")
    audit_fn("ERROR", symbol=symbol, reason="EXIT_LADDER_EXHAUSTED_NO_FILL", order_id=order_id)
    return ExitResult(False, "NO_FILL", 0, quantity, None, step + 1, order_id, operation_id)
