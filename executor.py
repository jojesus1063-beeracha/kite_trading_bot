"""
Order execution.

When cfg.PAPER_TRADING is True, no real orders are sent — signals are
just logged, which lets you dry-run the whole pipeline against live
market data before risking capital.
"""

import logging

logger = logging.getLogger("executor")


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


def place_entry_order(kite, symbol: str, direction: str, quantity: int, exchange: str, cfg):
    if quantity <= 0:
        logger.warning(f"Skipping order for {symbol} ({direction}): computed quantity is 0")
        return None

    if cfg.PAPER_TRADING:
        logger.info(f"[PAPER] {direction} {quantity} {exchange}:{symbol} @ MARKET")
        return {"order_id": "PAPER", "status": "PAPER_FILLED"}

    if getattr(cfg, "CHECK_MARGIN_BEFORE_ENTRY", True):
        ok, reason = _check_sufficient_margin(kite, symbol, direction, quantity, exchange, cfg)
        if not ok:
            logger.warning(f"Skipping order for {symbol} ({direction}): insufficient margin -- {reason}")
            return None

    transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
    order_id = kite.place_order(
        variety=cfg.VARIETY,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=cfg.PRODUCT,
        order_type=cfg.ORDER_TYPE_ENTRY,
        market_protection=cfg.MARKET_PROTECTION,  # required on MARKET/SL-M orders since Apr 2026
        # tag="<your registered algo/strategy tag if required>",
    )
    logger.info(f"[LIVE] Placed {direction} order {order_id} for {quantity} {exchange}:{symbol}")
    return {"order_id": order_id, "status": "SUBMITTED"}


def place_exit_order(kite, symbol: str, direction: str, quantity: int, exchange: str, cfg):
    """direction here is the ORIGINAL entry direction; the exit reverses it."""
    exit_direction = "SELL" if direction == "BUY" else "BUY"

    if cfg.PAPER_TRADING:
        logger.info(f"[PAPER] EXIT {exit_direction} {quantity} {exchange}:{symbol} @ MARKET")
        return {"order_id": "PAPER", "status": "PAPER_FILLED"}

    transaction_type = kite.TRANSACTION_TYPE_BUY if exit_direction == "BUY" else kite.TRANSACTION_TYPE_SELL
    order_id = kite.place_order(
        variety=cfg.VARIETY,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=cfg.PRODUCT,
        order_type="MARKET",
        market_protection=cfg.MARKET_PROTECTION,  # required on MARKET/SL-M orders since Apr 2026
    )
    logger.info(f"[LIVE] Placed EXIT order {order_id} for {quantity} {exchange}:{symbol}")
    return {"order_id": order_id, "status": "SUBMITTED"}
