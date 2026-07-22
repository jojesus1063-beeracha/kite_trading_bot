"""
Approximate round-trip trading costs for Zerodha intraday equity
(MIS) trades, based on their published rate structure as of mid-2026.

This is an ESTIMATE for backtesting purposes, not an exact
reproduction of your contract note — exchange transaction charges in
particular are rounded/tiered in ways this simplifies. For anything
where precision matters, check zerodha.com/charges directly. The
point here isn't perfect accuracy; it's making sure a backtest's
P&L isn't overstated by ignoring costs entirely, which is a much
bigger error than being a few rupees off on the exact fee.
"""

BROKERAGE_RATE = 0.0003    # 0.03% of turnover
BROKERAGE_CAP = 20.0       # or Rs 20 per executed order, whichever is LOWER
STT_SELL_RATE = 0.00025    # 0.025% on sell-side turnover only (intraday equity)
EXCHANGE_TXN_RATE = 0.0000297  # approx NSE exchange transaction charge, both sides
SEBI_CHARGES_RATE = 0.000001   # approx Rs 10/crore, both sides
GST_RATE = 0.18            # on brokerage + exchange txn charges + SEBI charges
STAMP_DUTY_BUY_RATE = 0.00003  # 0.003% on buy-side turnover only (intraday)


def estimate_trade_cost(buy_value: float, sell_value: float) -> float:
    """
    buy_value / sell_value: the rupee turnover on each side of a single
    round-trip trade (qty * price), regardless of whether the trade
    was a long or a short — the buy leg and sell leg both happen
    either way in an intraday trade.
    """
    brokerage = min(BROKERAGE_CAP, buy_value * BROKERAGE_RATE) + \
                min(BROKERAGE_CAP, sell_value * BROKERAGE_RATE)
    stt = sell_value * STT_SELL_RATE
    exchange_txn = (buy_value + sell_value) * EXCHANGE_TXN_RATE
    sebi_charges = (buy_value + sell_value) * SEBI_CHARGES_RATE
    gst = GST_RATE * (brokerage + exchange_txn + sebi_charges)
    stamp_duty = buy_value * STAMP_DUTY_BUY_RATE

    return brokerage + stt + exchange_txn + sebi_charges + gst + stamp_duty


def net_pnl_for_trade(direction: str, qty: int, entry: float, exit_price: float) -> dict:
    """
    Returns gross P&L, estimated costs, and net P&L for one trade.
    direction: "BUY" (long) or "SELL" (short) — the entry direction.
    """
    if direction == "BUY":
        buy_value = qty * entry
        sell_value = qty * exit_price
        gross_pnl = (exit_price - entry) * qty
    else:
        # A short: you sell first, buy back later -- but the buy-side
        # and sell-side turnover for cost purposes are the same either way.
        buy_value = qty * exit_price
        sell_value = qty * entry
        gross_pnl = (entry - exit_price) * qty

    costs = estimate_trade_cost(buy_value, sell_value)
    return {"gross_pnl": gross_pnl, "costs": costs, "net_pnl": gross_pnl - costs}
