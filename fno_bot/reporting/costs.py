"""
Approximate round-trip trading costs for Zerodha intraday INDEX OPTIONS
BUYING (not equity, not option selling), based on their published rate
structure as of mid-2026.

Structurally the same shape as the equity bot's costs.py (spec #19:
never evaluate on gross P&L alone), but the RATES are genuinely
different for options and must not be copied from the equity module:

- STT on options is charged on the SELL side, on the PREMIUM value,
  at a different rate than equity intraday STT.
- No stamp duty asymmetry the same way as equity (options attract
  stamp duty on buy-side premium turnover too, but at option-specific
  rates).
- Brokerage is typically flat-per-order for options at most discount
  brokers, not the equity 0.03%-capped-at-Rs20 structure.

This is an ESTIMATE for shadow/paper P&L purposes, not an exact
contract-note reproduction -- reconcile against real contract notes
before trusting it for LIVE-mode performance conclusions (spec #19).
Verify current rates directly against zerodha.com/charges before
relying on this for real capital decisions -- rates change.
"""

BROKERAGE_PER_ORDER = 20.0        # flat brokerage per executed order (buy leg + sell leg = 2 orders/trade)
STT_SELL_RATE = 0.001             # ~0.1% on sell-side premium turnover (options, intraday) -- VERIFY current rate
EXCHANGE_TXN_RATE = 0.0003503     # approx exchange transaction charge on premium turnover, both sides -- VERIFY
SEBI_CHARGES_RATE = 0.0000005     # approx SEBI turnover fee, both sides (index of Rs 10/crore-ish) -- VERIFY
GST_RATE = 0.18                   # on brokerage + exchange txn charges + SEBI charges
STAMP_DUTY_BUY_RATE = 0.00003     # approx 0.003% on buy-side premium turnover -- VERIFY


def estimate_trade_cost(buy_premium_value: float, sell_premium_value: float) -> float:
    """
    buy_premium_value / sell_premium_value: rupee turnover on each leg
    (qty * premium), for one round-trip options-buying trade.
    """
    brokerage = BROKERAGE_PER_ORDER * 2  # one order to enter, one to exit
    stt = sell_premium_value * STT_SELL_RATE
    exchange_txn = (buy_premium_value + sell_premium_value) * EXCHANGE_TXN_RATE
    sebi_charges = (buy_premium_value + sell_premium_value) * SEBI_CHARGES_RATE
    gst = GST_RATE * (brokerage + exchange_txn + sebi_charges)
    stamp_duty = buy_premium_value * STAMP_DUTY_BUY_RATE

    return brokerage + stt + exchange_txn + sebi_charges + gst + stamp_duty


def net_pnl_for_trade(qty: int, entry_premium: float, exit_premium: float) -> dict:
    """
    Options buying is always a long premium position (CE or PE) in
    V1 -- no naked selling, so unlike the equity module there's no
    direction branch here. gross_pnl is simply (exit - entry) * qty.
    """
    buy_value = qty * entry_premium
    sell_value = qty * exit_premium
    gross_pnl = (exit_premium - entry_premium) * qty

    costs = estimate_trade_cost(buy_value, sell_value)
    return {"gross_pnl": gross_pnl, "costs": costs, "net_pnl": gross_pnl - costs}
