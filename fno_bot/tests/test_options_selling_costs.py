from fno_bot.strategies.premium_rotation_session import ClosedTrade
from fno_bot.strategies.premium_rotation_costs_log import net_pnl_for_closed_trade

def test_short_trade_profit_is_entry_minus_exit():
    trade = ClosedTrade("PE", 100.0, 0.0, 80.0, 10.0, "PROFIT_TARGET", 50, 20.0, -5.0, side="SHORT")
    result = net_pnl_for_closed_trade(trade)
    assert result["gross_pnl"] == 1000.0

def test_short_trade_loss_is_exit_minus_entry():
    trade = ClosedTrade("CE", 100.0, 0.0, 120.0, 10.0, "HARD_STOP", 50, 0.0, -20.0, side="SHORT")
    result = net_pnl_for_closed_trade(trade)
    assert result["gross_pnl"] == -1000.0
