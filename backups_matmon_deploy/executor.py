"""
Order execution.

When cfg.PAPER_TRADING is True, no real orders are sent — signals are
just logged, which lets you dry-run the whole pipeline against live
market data before risking capital.
"""

import logging
import time
import uuid

logger = logging.getLogger("executor")


def make_entry_tag() -> str:
    """Return a unique Kite-compatible identity for one entry intent."""

    tag = "KBE" + uuid.uuid4().hex[:17]

    if len(tag) != 20 or not tag.isalnum():
        raise RuntimeError("Generated entry tag is invalid")

    return tag


def reconcile_entry_submission(
    kite,
    *,
    client_tag,
    symbol,
    exchange,
    direction,
    quantity,
    product,
    order_type,
    max_wait_seconds=0,
    poll_interval_seconds=1,
    sleep_fn=None,
    clock_fn=None,
):
    """Recover a uniquely tagged entry after a lost submit response.

    A tag match alone is not enough: all immutable order details must
    agree.  Zero or multiple matches remain unresolved and are never
    converted into a second submission.
    """

    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic
    started = clock_fn()
    attempts = 0
    api_errors = 0
    last_error = None

    expected = {
        "tag": str(client_tag),
        "symbol": str(symbol).upper(),
        "exchange": str(exchange).upper(),
        "side": str(direction).upper(),
        "quantity": int(quantity),
        "product": str(product).upper(),
        "order_type": str(order_type).upper(),
    }

    while True:
        attempts += 1

        try:
            broker_orders = kite.orders()
        except Exception as exc:
            broker_orders = []
            api_errors += 1
            last_error = str(exc)

        matches = []

        for order in broker_orders or []:
            try:
                if str(order.get("tag") or "") != expected["tag"]:
                    continue
                if str(order.get("tradingsymbol") or "").upper() != expected["symbol"]:
                    continue
                if str(order.get("exchange") or "").upper() != expected["exchange"]:
                    continue
                if str(order.get("transaction_type") or "").upper() != expected["side"]:
                    continue
                if int(order.get("quantity") or 0) != expected["quantity"]:
                    continue
                if str(order.get("product") or "").upper() != expected["product"]:
                    continue
                if str(order.get("order_type") or "").upper() != expected["order_type"]:
                    continue
                if not order.get("order_id"):
                    continue
                matches.append(order)
            except (TypeError, ValueError, AttributeError):
                continue

        if len(matches) == 1:
            return {
                "matched": True,
                "ambiguous": False,
                "order_id": str(matches[0]["order_id"]),
                "order": matches[0],
                "attempts": attempts,
                "api_error_count": api_errors,
                "last_error": last_error,
            }

        if len(matches) > 1:
            return {
                "matched": False,
                "ambiguous": True,
                "order_id": None,
                "order": None,
                "attempts": attempts,
                "api_error_count": api_errors,
                "last_error": "multiple broker orders matched the entry tag",
            }

        if clock_fn() - started >= max_wait_seconds:
            return {
                "matched": False,
                "ambiguous": False,
                "order_id": None,
                "order": None,
                "attempts": attempts,
                "api_error_count": api_errors,
                "last_error": last_error,
            }

        sleep_fn(poll_interval_seconds)


def _check_sufficient_margin(kite, symbol, direction, quantity, exchange, cfg):
    """
    Verifies real available margin against what this specific order would
    actually require, using Kite's own order_margins() calculator (which
    accounts for the real MIS leverage for this symbol, not a rough guess).

    Returns (ok, reason). If the check itself fails for any reason (API
    hiccup, unexpected response shape), fails OPEN with a logged warning --
    a margin-check outage should not silently halt all trading, since the
    broker's own order placement will still reject an underfunded order
    at that point anyway; this check exists to catch it EARLIER and more
    cheaply, not as the only safety net.
    """
    try:
        transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_params = [{
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": transaction_type,
            "variety": cfg.VARIETY,
            "product": cfg.PRODUCT,
            "order_type": cfg.ORDER_TYPE_ENTRY,
            "quantity": quantity,
            "price": 0,
            "trigger_price": 0,
        }]
        margin_result = kite.order_margins(order_params)
        required = margin_result[0].get("total")
        if required is None:
            logger.warning(f"Margin check for {symbol}: unexpected response shape, proceeding without check")
            return True, None

        margins = kite.margins()
        available = margins.get("equity", {}).get("net")
        if available is None:
            logger.warning(f"Margin check for {symbol}: could not read available balance, proceeding without check")
            return True, None

        if required > available:
            return False, f"required Rs{required:.2f} > available Rs{available:.2f}"
        return True, None
    except Exception as e:
        logger.warning(f"Margin check for {symbol} failed ({e}), proceeding without check")
        return True, None


def get_live_available_margin(kite) -> float | None:
    """Returns real-time available equity margin (Kite's 'net' figure,
    which correctly reflects funds already locked in other open
    positions), or None if the call fails for any reason."""
    try:
        margins = kite.margins()
        net = margins.get("equity", {}).get("net")
        return float(net) if net is not None else None
    except Exception:
        return None


def cap_quantity_by_margin(kite, symbol: str, direction: str, quantity: int, exchange: str, cfg) -> int:
    """
    Caps `quantity` so the REAL margin it would require never exceeds
    MAX_POSITION_SIZE_PCT of LIVE available margin (not the static
    CAPITAL config value). Using live available margin -- rather than
    the configured capital -- means this correctly accounts for funds
    already locked into OTHER currently-open positions, so the cap
    tightens automatically as more capital gets deployed elsewhere,
    instead of assuming the full configured capital is always free.

    Formula:
        available_margin = get_live_available_margin()
        margin_budget = available_margin * MAX_POSITION_SIZE_PCT / 100
        qty = floor(margin_budget / required_margin_per_share)

    required_margin_per_share is queried directly via order_margins()
    with quantity=1, rather than scaled proportionally from an
    arbitrary quantity -- more precise, and avoids assuming linearity.

    Never INCREASES the passed-in quantity (which already reflects
    risk-based sizing) -- only ever reduces it, or leaves it unchanged.

    Fails open (returns the original quantity unchanged) on any error --
    a margin-check outage should not silently block trading on its own;
    the final order_margins-based _check_sufficient_margin() call still
    guards against genuinely insufficient funds regardless.
    """
    max_pct = getattr(cfg, "MAX_POSITION_SIZE_PCT", None)
    if max_pct is None or quantity <= 0:
        return quantity

    try:
        available_margin = get_live_available_margin(kite)
        if available_margin is None:
            logger.warning(f"Margin-based cap for {symbol}: could not read live available margin, using uncapped quantity")
            return quantity

        margin_budget = available_margin * max_pct / 100

        transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_params = [{
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": transaction_type,
            "variety": cfg.VARIETY,
            "product": cfg.PRODUCT,
            "order_type": cfg.ORDER_TYPE_ENTRY,
            "quantity": 1,
            "price": 0,
            "trigger_price": 0,
        }]
        margin_result = kite.order_margins(order_params)
        required_margin_per_share = margin_result[0].get("total")
        if required_margin_per_share is None or required_margin_per_share <= 0:
            return quantity

        max_qty_by_margin = int(margin_budget / required_margin_per_share)
        capped_qty = min(quantity, max_qty_by_margin)

        if capped_qty < quantity:
            logger.info(f"{symbol}: margin cap applied -- live available Rs{available_margin:.2f}, "
                        f"budget Rs{margin_budget:.2f} ({max_pct}% of live margin), "
                        f"Rs{required_margin_per_share:.2f}/share -> reduced {quantity} to {capped_qty} shares")
        return max(capped_qty, 0)
    except Exception as e:
        logger.warning(f"Margin-based position cap for {symbol} failed ({e}), using uncapped quantity")
        return quantity


def place_entry_order(
    kite,
    symbol: str,
    direction: str,
    quantity: int,
    exchange: str,
    cfg,
    entry_plan=None,
):
    """
    Stage 3: entries are only ever tracked based on a BROKER-CONFIRMED
    fill, never on kite.place_order()'s return value alone (which only
    confirms submission). Full lifecycle: create a durable ENTRY intent
    BEFORE submission -> submit -> attach the broker order_id
    IMMEDIATELY (before verification begins) -> verify_order_execution()
    -> track only the confirmed filled_quantity.

    ALWAYS returns a dict: {"success": bool, "order_id": str|None,
    "operation_id": str|None, "status": str, "reason": str|None,
    "requested_quantity": int, "filled_quantity": int,
    "average_price": float|None, "entry_confirmation_pending": bool,
    "resolved": bool}. "success" is True only when filled_quantity > 0
    -- callers must track exactly filled_quantity, never requested_quantity.
    """
    def _rejected(reason, operation_id=None):
        logger.warning(f"Skipping order for {symbol} ({direction}): {reason}")
        return {"success": False, "order_id": None, "operation_id": operation_id, "status": "REJECTED",
                "reason": reason, "requested_quantity": quantity, "filled_quantity": 0,
                "average_price": None, "entry_confirmation_pending": False, "resolved": True}

    if quantity <= 0:
        return _rejected("computed quantity is 0")

    if cfg.PAPER_TRADING:
        # Paper mode is completely unchanged: no order_history/pending-order-store
        # calls at all, average_price stays None so callers fall back to the
        # existing signal-price behavior exactly as before Stage 3.
        logger.info(f"[PAPER] {direction} {quantity} {exchange}:{symbol} @ MARKET")
        return {"success": True, "order_id": "PAPER", "operation_id": None, "status": "PAPER_FILLED",
                "reason": None, "requested_quantity": quantity, "filled_quantity": quantity,
                "average_price": None, "entry_confirmation_pending": False, "resolved": True}

    if getattr(cfg, "CHECK_MARGIN_BEFORE_ENTRY", True):
        ok, margin_reason = _check_sufficient_margin(kite, symbol, direction, quantity, exchange, cfg)
        if not ok:
            return _rejected(f"insufficient margin -- {margin_reason}")

    if getattr(cfg, "CIRCUIT_PROXIMITY_PCT", None) is not None:
        from risk_manager import is_near_circuit_limit
        try:
            quote = kite.quote([f"{exchange}:{symbol}"])
            q = quote.get(f"{exchange}:{symbol}", {})
            last_price = q.get("last_price")
            lower_limit = q.get("lower_circuit_limit")
            upper_limit = q.get("upper_circuit_limit")
            if is_near_circuit_limit(direction, last_price, lower_limit, upper_limit, cfg.CIRCUIT_PROXIMITY_PCT):
                reason = (f"within {cfg.CIRCUIT_PROXIMITY_PCT}% of circuit limit "
                          f"(price={last_price}, lower={lower_limit}, upper={upper_limit})")
                return _rejected(reason)
        except Exception as e:
            logger.warning(f"Circuit-proximity check for {symbol} failed ({e}), proceeding without check")

    from pending_order_store import (create_order_intent, attach_broker_order_id,
                                      update_order_verification, mark_order_resolved,
                                      has_unresolved_order, UnresolvedOrderExistsError)
    from order_verification import verify_order_execution

    if has_unresolved_order(symbol, exchange, "ENTRY"):
        return _rejected("ENTRY_BLOCKED_PENDING_ORDER")

    client_tag = make_entry_tag()

    try:
        operation_id = create_order_intent(
            symbol,
            exchange,
            "ENTRY",
            direction,
            quantity,
            client_tag=client_tag,
            metadata=entry_plan,
        )
    except UnresolvedOrderExistsError:
        return _rejected("ENTRY_BLOCKED_PENDING_ORDER")
    except Exception as e:
        return _rejected(f"intent creation failed: {e}")

    transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
    try:
        order_id = kite.place_order(
            variety=cfg.VARIETY,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=cfg.PRODUCT,
            order_type=cfg.ORDER_TYPE_ENTRY,
            market_protection=cfg.MARKET_PROTECTION,
            tag=client_tag,
        )
    except Exception as e:
        # Submission outcome is genuinely UNCERTAIN -- the network call could
        # have failed after the broker already accepted the order. Never
        # resubmit blindly. The intent stays unresolved (order_id=None) for
        # deliberate reconciliation/recovery, not automatic retry.
        logger.error(f"CRITICAL: order submission uncertain for {symbol} "
                     f"(operation_id={operation_id}, tag={client_tag}): {e}")

        reconciliation = reconcile_entry_submission(
            kite,
            client_tag=client_tag,
            symbol=symbol,
            exchange=exchange,
            direction=direction,
            quantity=quantity,
            product=cfg.PRODUCT,
            order_type=cfg.ORDER_TYPE_ENTRY,
            max_wait_seconds=getattr(
                cfg,
                "ENTRY_RECONCILE_MAX_WAIT_SECONDS",
                0,
            ),
            poll_interval_seconds=getattr(
                cfg,
                "ENTRY_RECONCILE_POLL_INTERVAL_SECONDS",
                1,
            ),
        )

        if not reconciliation["matched"]:
            status = (
                "SUBMISSION_AMBIGUOUS"
                if reconciliation["ambiguous"]
                else "SUBMISSION_UNCERTAIN"
            )
            return {
                "success": False,
                "order_id": None,
                "operation_id": operation_id,
                "client_tag": client_tag,
                "status": status,
                "reason": (
                    "submission exception; broker outcome remains unresolved: "
                    f"{e}"
                ),
                "requested_quantity": quantity,
                "filled_quantity": 0,
                "average_price": None,
                "entry_confirmation_pending": True,
                "resolved": False,
            }

        order_id = reconciliation["order_id"]
        logger.warning(
            f"Recovered entry order {order_id} for {symbol} using tag={client_tag}"
        )

    try:
        attach_broker_order_id(operation_id, order_id)
    except Exception as exc:
        logger.critical(
            f"Entry order ID persistence uncertain for {symbol}: "
            f"order={order_id} operation={operation_id}: {exc}"
        )
        return {
            "success": False,
            "order_id": str(order_id),
            "operation_id": operation_id,
            "client_tag": client_tag,
            "status": "PERSISTENCE_UNCERTAIN",
            "reason": str(exc),
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
            "entry_confirmation_pending": True,
            "resolved": False,
        }
    logger.info(f"[LIVE] Placed {direction} order {order_id} for {quantity} {exchange}:{symbol} "
                f"(operation_id={operation_id})")

    exec_result = verify_order_execution(
        kite, order_id, quantity,
        max_wait_seconds=getattr(cfg, "ORDER_VERIFY_MAX_WAIT_SECONDS", 15),
        poll_interval_seconds=getattr(cfg, "ORDER_VERIFY_POLL_INTERVAL_SECONDS", 1),
    )
    update_order_verification(operation_id, exec_result)

    filled = exec_result.filled_quantity
    if exec_result.terminal:
        mark_order_resolved(operation_id, resolution_reason=exec_result.status)

    if exec_result.status == "COMPLETE":
        logger.info(f"ENTRY CONFIRMED: {symbol} {direction} {filled}@{exec_result.average_price}")
        return {"success": True, "order_id": order_id, "operation_id": operation_id,
                "client_tag": client_tag, "status": "COMPLETE",
                "reason": None, "requested_quantity": quantity, "filled_quantity": filled,
                "average_price": exec_result.average_price, "entry_confirmation_pending": False, "resolved": True}

    if exec_result.status == "PARTIALLY_FILLED" and exec_result.terminal:
        logger.info(f"ENTRY CONFIRMED (terminal partial fill): {symbol} {direction} "
                    f"{filled}/{quantity}@{exec_result.average_price}")
        return {"success": True, "order_id": order_id, "operation_id": operation_id,
                "client_tag": client_tag, "status": "PARTIALLY_FILLED",
                "reason": "terminal partial fill", "requested_quantity": quantity, "filled_quantity": filled,
                "average_price": exec_result.average_price, "entry_confirmation_pending": False, "resolved": True}

    if exec_result.status in ("REJECTED", "CANCELLED"):
        logger.warning(f"ENTRY {exec_result.status}: {symbol} {direction} -- {exec_result.status_message}")
        return {"success": False, "order_id": order_id, "operation_id": operation_id,
                "client_tag": client_tag, "status": exec_result.status,
                "reason": exec_result.status_message, "requested_quantity": quantity, "filled_quantity": 0,
                "average_price": None, "entry_confirmation_pending": False, "resolved": True}

    # TIMEOUT or UNKNOWN -- not terminal. If any real shares are already
    # confirmed filled, they exist and must be tracked; the remainder stays
    # unresolved for later recovery, never assumed either way.
    if filled > 0:
        logger.warning(f"ENTRY CONFIRMATION PENDING: {symbol} {direction} -- {filled}/{quantity} "
                       f"confirmed so far, remainder still unresolved ({exec_result.status})")
        return {"success": True, "order_id": order_id, "operation_id": operation_id,
                "client_tag": client_tag, "status": exec_result.status,
                "reason": "partial fill confirmed, remainder still pending", "requested_quantity": quantity,
                "filled_quantity": filled, "average_price": exec_result.average_price,
                "entry_confirmation_pending": True, "resolved": False}

    logger.warning(f"ENTRY CONFIRMATION PENDING: {symbol} {direction} -- no fill confirmed yet ({exec_result.status})")
    return {"success": False, "order_id": order_id, "operation_id": operation_id,
            "client_tag": client_tag, "status": exec_result.status,
            "reason": "no confirmed fill yet, order still unresolved", "requested_quantity": quantity,
            "filled_quantity": 0, "average_price": None, "entry_confirmation_pending": True, "resolved": False}



def _place_exit_order(
    kite,
    symbol: str,
    direction: str,
    quantity: int,
    exchange: str,
    cfg,
    action: str,
    protection_clearance=None,
):
    """
    Shared verified execution for normal EXIT and FORCE_EXIT orders.

    `direction` is the ORIGINAL position direction. The exit reverses it.

    A live exit is never treated as completed merely because
    kite.place_order() returned an order ID.

    Lifecycle:
      1. Persist EXIT intent.
      2. Submit broker order.
      3. Persist broker order ID immediately.
      4. Verify using order_history().
      5. Return only confirmed filled quantity and broker average price.

    Live callers must also provide a clearance bound to this exact order.
    The clearance proves that the broker protective stop reached a
    terminal state before a potentially competing market exit is sent.
    """

    if action not in ("EXIT", "FORCE_EXIT"):
        raise ValueError(
            f"Unsupported exit action: {action}"
        )

    exit_direction = (
        "SELL" if direction == "BUY" else "BUY"
    )
    action_label = (
        "FORCE EXIT"
        if action == "FORCE_EXIT"
        else "EXIT"
    )

    def rejected(reason, operation_id=None):
        logger.warning(
            f"Skipping {action_label} for {symbol} ({exit_direction}): {reason}"
        )
        return {
            "success": False,
            "order_id": None,
            "operation_id": operation_id,
            "status": "REJECTED",
            "reason": reason,
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
            "exit_confirmation_pending": False,
            "resolved": True,
        }

    def blocked_pending():
        logger.warning(
            f"Skipping {action_label} for {symbol}: {action}_BLOCKED_PENDING_ORDER"
        )
        return {
            "success": False,
            "order_id": None,
            "operation_id": None,
            "status": f"{action}_BLOCKED_PENDING_ORDER",
            "reason": "an unresolved exit already exists",
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
            "exit_confirmation_pending": True,
            "resolved": False,
        }

    if quantity <= 0:
        return rejected("exit quantity is 0")

    # Paper mode remains completely synthetic:
    # no broker submission, no order_history and no pending-order store.
    if cfg.PAPER_TRADING:
        logger.info(
            f"[PAPER] {action_label} {exit_direction} "
            f"{quantity} {exchange}:{symbol} @ MARKET"
        )
        return {
            "success": True,
            "order_id": "PAPER",
            "operation_id": None,
            "status": "PAPER_FILLED",
            "reason": None,
            "requested_quantity": quantity,
            "filled_quantity": quantity,
            "average_price": None,
            "exit_confirmation_pending": False,
            "resolved": True,
        }

    from protective_stop_exit import valid_exit_clearance

    if not valid_exit_clearance(
        protection_clearance,
        symbol=symbol,
        exchange=exchange,
        quantity=quantity,
        exit_action=action,
    ):
        return rejected(
            f"{action}_BLOCKED_PROTECTIVE_STOP_NOT_CLEARED"
        )

    from pending_order_store import (
        create_order_intent,
        attach_broker_order_id,
        update_order_verification,
        has_unresolved_order,
        UnresolvedOrderExistsError,
    )
    from order_verification import verify_order_execution

    # EXIT and FORCE_EXIT share one lock family in pending_order_store.
    if has_unresolved_order(symbol, exchange, action):
        return blocked_pending()

    try:
        operation_id = create_order_intent(
            symbol=symbol,
            exchange=exchange,
            action=action,
            side=exit_direction,
            requested_quantity=quantity,
        )
    except UnresolvedOrderExistsError:
        return blocked_pending()
    except Exception as exc:
        return rejected(f"{action_label.lower()} intent creation failed: {exc}")

    transaction_type = (
        kite.TRANSACTION_TYPE_BUY
        if exit_direction == "BUY"
        else kite.TRANSACTION_TYPE_SELL
    )

    try:
        order_id = kite.place_order(
            variety=cfg.VARIETY,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=cfg.PRODUCT,
            order_type="MARKET",
            market_protection=cfg.MARKET_PROTECTION,
        )
    except Exception as exc:
        # The broker may have accepted the order even though the network
        # response failed. Keep the intent unresolved and never retry blindly.
        logger.error(
            f"CRITICAL: {action_label} submission uncertain for {symbol} "
            f"(operation_id={operation_id}): {exc}"
        )
        return {
            "success": False,
            "order_id": None,
            "operation_id": operation_id,
            "status": "SUBMISSION_UNCERTAIN",
            "reason": (
                "exit submission exception; broker outcome unknown: "
                f"{exc}"
            ),
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
            "exit_confirmation_pending": True,
            "resolved": False,
        }

    # This must happen before the first order_history() call.
    attach_broker_order_id(operation_id, order_id)

    logger.info(
        f"[LIVE] Placed {action_label} {exit_direction} order {order_id} "
        f"for {quantity} {exchange}:{symbol} "
        f"(operation_id={operation_id})"
    )

    exec_result = verify_order_execution(
        kite,
        order_id,
        quantity,
        max_wait_seconds=getattr(
            cfg,
            "ORDER_VERIFY_MAX_WAIT_SECONDS",
            15,
        ),
        poll_interval_seconds=getattr(
            cfg,
            "ORDER_VERIFY_POLL_INTERVAL_SECONDS",
            1,
        ),
    )

    update_order_verification(operation_id, exec_result)

    filled = exec_result.filled_quantity

    if exec_result.status == "COMPLETE":
        logger.info(
            f"{action_label} CONFIRMED: {symbol} {exit_direction} "
            f"{filled}@{exec_result.average_price}"
        )
        return {
            "success": True,
            "order_id": order_id,
            "operation_id": operation_id,
            "status": "COMPLETE",
            "reason": None,
            "requested_quantity": quantity,
            "filled_quantity": filled,
            "average_price": exec_result.average_price,
            "exit_confirmation_pending": False,
            "resolved": True,
        }

    if (
        exec_result.status == "PARTIALLY_FILLED"
        and exec_result.terminal
    ):
        logger.info(
            f"{action_label} CONFIRMED (terminal partial fill): "
            f"{symbol} {exit_direction} "
            f"{filled}/{quantity}@{exec_result.average_price}"
        )
        return {
            "success": True,
            "order_id": order_id,
            "operation_id": operation_id,
            "status": "PARTIALLY_FILLED",
            "reason": "terminal partial exit fill",
            "requested_quantity": quantity,
            "filled_quantity": filled,
            "average_price": exec_result.average_price,
            "exit_confirmation_pending": False,
            "resolved": True,
        }

    if exec_result.status in ("REJECTED", "CANCELLED"):
        logger.warning(
            f"{action_label} {exec_result.status}: {symbol} "
            f"{exit_direction} -- {exec_result.status_message}"
        )
        return {
            "success": False,
            "order_id": order_id,
            "operation_id": operation_id,
            "status": exec_result.status,
            "reason": exec_result.status_message,
            "requested_quantity": quantity,
            "filled_quantity": 0,
            "average_price": None,
            "exit_confirmation_pending": False,
            "resolved": True,
        }

    # TIMEOUT or UNKNOWN:
    # confirmed shares must be acted on, but the remainder remains unresolved.
    if filled > 0:
        logger.warning(
            f"{action_label} CONFIRMATION PENDING: {symbol} "
            f"{exit_direction} -- {filled}/{quantity} confirmed; "
            f"remainder unresolved ({exec_result.status})"
        )
        return {
            "success": True,
            "order_id": order_id,
            "operation_id": operation_id,
            "status": exec_result.status,
            "reason": (
                "partial exit fill confirmed; remainder unresolved"
            ),
            "requested_quantity": quantity,
            "filled_quantity": filled,
            "average_price": exec_result.average_price,
            "exit_confirmation_pending": True,
            "resolved": False,
        }

    logger.warning(
        f"{action_label} CONFIRMATION PENDING: {symbol} {exit_direction} -- "
        f"no fill confirmed ({exec_result.status})"
    )
    return {
        "success": False,
        "order_id": order_id,
        "operation_id": operation_id,
        "status": exec_result.status,
        "reason": "no confirmed exit fill; order remains unresolved",
        "requested_quantity": quantity,
        "filled_quantity": 0,
        "average_price": None,
        "exit_confirmation_pending": True,
        "resolved": False,
    }


def place_exit_order(
    kite,
    symbol: str,
    direction: str,
    quantity: int,
    exchange: str,
    cfg,
    *,
    protection_clearance=None,
):
    """Submit and verify a normal strategy-triggered exit."""
    return _place_exit_order(
        kite,
        symbol,
        direction,
        quantity,
        exchange,
        cfg,
        action="EXIT",
        protection_clearance=protection_clearance,
    )


def place_force_exit_order(
    kite,
    symbol: str,
    direction: str,
    quantity: int,
    exchange: str,
    cfg,
    *,
    protection_clearance=None,
):
    """Submit and verify an end-of-day forced exit."""
    return _place_exit_order(
        kite,
        symbol,
        direction,
        quantity,
        exchange,
        cfg,
        action="FORCE_EXIT",
        protection_clearance=protection_clearance,
    )
