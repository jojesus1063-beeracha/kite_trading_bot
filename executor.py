"""
Order execution.

When cfg.PAPER_TRADING is True, no real orders are sent — signals are
just logged, which lets you dry-run the whole pipeline against live
market data before risking capital.
"""

import logging

logger = logging.getLogger("executor")


def place_entry_order(kite, symbol: str, direction: str, quantity: int, exchange: str, cfg):
    if quantity <= 0:
        logger.warning(f"Skipping order for {symbol}: computed quantity is 0")
        return None

    if cfg.PAPER_TRADING:
        logger.info(f"[PAPER] {direction} {quantity} {exchange}:{symbol} @ MARKET")
        return {"order_id": "PAPER", "status": "PAPER_FILLED"}

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
