"""
Replays today's 5 REAL trades (same entry/stop/exit prices, same
direction) through the position-sizing pipeline at CAPITAL=100000,
to see what would have actually happened with more capital -- not a
simple proportional scale-up, since MAX_POSITION_SIZE_PCT and margin
requirements don't scale linearly.
"""
import config as cfg
from auth import get_kite_client
from executor import cap_quantity_by_margin
from costs import net_pnl_for_trade

kite = get_kite_client()

# Today's real trades: (symbol, direction, entry, stop, exit, exchange)
trades = [
    ("BANDHANBNK", "BUY", 168.81, 168.26, 168.02, "NSE"),
    ("IDFCFIRSTB", "BUY", 86.31, 85.67, 85.63, "NSE"),
    ("HUDCO",      "BUY", 199.51, 197.69, 196.96, "NSE"),
    ("BRIGADE",    "BUY", 558.80, 556.86, 560.70, "NSE"),
    ("ATGL",       "SELL", 649.90, 651.43, 645.90, "NSE"),
]

# Simulated config: same everything, but CAPITAL raised to 1,00,000
class SimCfg:
    CAPITAL = 100000
    RISK_PER_TRADE_PCT = cfg.RISK_PER_TRADE_PCT
    MAX_POSITION_SIZE_PCT = cfg.MAX_POSITION_SIZE_PCT
    VARIETY = cfg.VARIETY
    PRODUCT = cfg.PRODUCT
    ORDER_TYPE_ENTRY = cfg.ORDER_TYPE_ENTRY

print(f"Simulating at CAPITAL=Rs{SimCfg.CAPITAL:,}, RISK_PER_TRADE_PCT={SimCfg.RISK_PER_TRADE_PCT}%, "
      f"MAX_POSITION_SIZE_PCT={SimCfg.MAX_POSITION_SIZE_PCT}%")
print("(Margin cap assumes full Rs1,00,000 is available -- i.e. no other position already using margin)")
print()

risk_amount = SimCfg.CAPITAL * SimCfg.RISK_PER_TRADE_PCT / 100
total_gross = 0
total_net = 0

for symbol, direction, entry, stop, exit_price, exchange in trades:
    per_share_risk = abs(entry - stop)
    qty_risk_based = int(risk_amount / per_share_risk)

    # Margin-based cap: query real order_margins for qty=1, then apply
    # against a HYPOTHETICAL Rs100,000 available margin (since this
    # simulation assumes full funding, not today's real ~Rs492 balance)
    try:
        transaction_type = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_params = [{
            "exchange": exchange, "tradingsymbol": symbol,
            "transaction_type": transaction_type, "variety": SimCfg.VARIETY,
            "product": SimCfg.PRODUCT, "order_type": SimCfg.ORDER_TYPE_ENTRY,
            "quantity": 1, "price": 0, "trigger_price": 0,
        }]
        margin_per_share = kite.order_margins(order_params)[0].get("total", 0)
    except Exception as e:
        print(f"{symbol}: margin lookup failed ({e}), using risk-based qty only")
        margin_per_share = 0

    if margin_per_share > 0:
        budget = SimCfg.CAPITAL * SimCfg.MAX_POSITION_SIZE_PCT / 100
        max_qty_by_margin = int(budget / margin_per_share)
        final_qty = min(qty_risk_based, max_qty_by_margin)
    else:
        final_qty = qty_risk_based

    pnl_per_share = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    gross_pnl = pnl_per_share * final_qty
    cost_result = net_pnl_for_trade(direction, final_qty, entry, exit_price)
    net_pnl = cost_result["net_pnl"]

    total_gross += gross_pnl
    total_net += net_pnl

    print(f"{symbol:12s} {direction:4s} qty(risk-based)={qty_risk_based:5d}  "
          f"margin/share=Rs{margin_per_share:8.2f}  final_qty={final_qty:5d}  "
          f"gross={gross_pnl:+9.2f}  net={net_pnl:+9.2f}")

print()
print(f"TODAY'S ACTUAL result (Rs500 capital):  net Rs-7.59")
print(f"SIMULATED at Rs1,00,000 capital:        gross Rs{total_gross:+.2f}   net Rs{total_net:+.2f}")
