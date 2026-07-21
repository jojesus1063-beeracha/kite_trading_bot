"""
Risk management: position sizing and the daily kill-switch.

This module tracks state for the trading day (trade count, realized
P&L) and decides whether a new signal is allowed to be traded.
"""

from dataclasses import dataclass, field


@dataclass
class DayState:
    trades_taken: int = 0
    realized_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.day = DayState()

    def max_loss_amount(self) -> float:
        return self.cfg.CAPITAL * self.cfg.MAX_DAILY_LOSS_PCT / 100

    def risk_amount_per_trade(self) -> float:
        return self.cfg.CAPITAL * self.cfg.RISK_PER_TRADE_PCT / 100

    def position_size(self, entry_price: float, stop_loss: float) -> int:
        """Quantity such that (entry - stop) * qty ~= risk_amount_per_trade."""
        per_share_risk = abs(entry_price - stop_loss)
        if per_share_risk <= 0:
            return 0
        qty = int(self.risk_amount_per_trade() / per_share_risk)
        return max(qty, 0)

    def can_take_new_trade(self) -> bool:
        if self.day.halted:
            return False
        if self.day.trades_taken >= self.cfg.MAX_TRADES_PER_DAY:
            self._halt(f"Max trades per day ({self.cfg.MAX_TRADES_PER_DAY}) reached")
            return False
        if self.day.realized_pnl <= -self.max_loss_amount():
            self._halt(f"Daily loss limit ({self.cfg.MAX_DAILY_LOSS_PCT}% of capital) hit")
            return False
        return True

    def record_trade_result(self, pnl: float):
        self.day.trades_taken += 1
        self.day.realized_pnl += pnl
        if self.day.realized_pnl <= -self.max_loss_amount():
            self._halt(f"Daily loss limit ({self.cfg.MAX_DAILY_LOSS_PCT}% of capital) hit")

    def _halt(self, reason: str):
        self.day.halted = True
        self.day.halt_reason = reason

    def reset_for_new_day(self):
        self.day = DayState()
