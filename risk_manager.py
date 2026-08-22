"""
Risk management: position sizing and the daily kill-switch.

This module tracks state for the trading day (trade count, realized
P&L) and decides whether a new signal is allowed to be traded.

The risk budget is deliberately prospective as well as retrospective:
a new trade can be rejected before entry when realized losses + open
stop risk + the proposed trade risk would exceed the daily loss budget.
"""

import json
import os
from datetime import date
from dataclasses import dataclass

DAY_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "day_state.json")


def _save_day_state(day: "DayState"):
    data = {
        "date": date.today().isoformat(),
        "trades_taken": day.trades_taken,
        "realized_pnl": day.realized_pnl,
        "halted": day.halted,
        "halt_reason": day.halt_reason,
    }
    with open(DAY_STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _load_day_state() -> "DayState":
    if not os.path.exists(DAY_STATE_PATH):
        return DayState()
    try:
        with open(DAY_STATE_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return DayState()
    if data.get("date") != date.today().isoformat():
        return DayState()
    return DayState(
        trades_taken=data.get("trades_taken", 0),
        realized_pnl=data.get("realized_pnl", 0.0),
        halted=data.get("halted", False),
        halt_reason=data.get("halt_reason", ""),
    )


@dataclass
class DayState:
    trades_taken: int = 0
    realized_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg, persist: bool = True):
        """
        persist=True  (default, used by main.py / live trading): loads
            and saves day_state.json.
        persist=False (used by backtest.py): starts with a fresh
            in-memory-only DayState and NEVER reads or writes
            day_state.json.
        """
        self.cfg = cfg
        self.persist = persist
        self.day = _load_day_state() if persist else DayState()

    def max_loss_amount(self) -> float:
        return self.cfg.CAPITAL * self.cfg.MAX_DAILY_LOSS_PCT / 100

    def risk_amount_per_trade(self) -> float:
        return self.cfg.CAPITAL * self.cfg.RISK_PER_TRADE_PCT / 100

    def position_size(self, entry_price: float, stop_loss: float) -> int:
        """Return quantity whose stop distance approximately equals the
        configured per-trade risk budget. Margin/exposure capping remains
        executor responsibility because broker margin is more accurate
        than notional value for MIS positions.
        """
        per_share_risk = abs(entry_price - stop_loss)
        if per_share_risk <= 0:
            return 0
        qty = int(self.risk_amount_per_trade() / per_share_risk)
        return max(qty, 0)

    @staticmethod
    def position_stop_risk(position: dict) -> float:
        """Worst-case loss from current recorded entry to hard stop.

        A stop already beyond breakeven contributes zero loss risk. Missing
        or invalid stop/entry information fails safe by returning infinity;
        an unknown stop must never make a new trade appear affordable.
        """
        try:
            qty = int(position.get("qty", 0) or 0)
            entry = float(position.get("entry"))
            stop = float(position.get("stop"))
            direction = position.get("direction")
        except (TypeError, ValueError):
            return float("inf")

        if qty <= 0 or entry <= 0 or stop <= 0:
            return float("inf")
        if direction == "BUY":
            return max(0.0, entry - stop) * qty
        if direction == "SELL":
            return max(0.0, stop - entry) * qty
        return float("inf")

    def open_stop_risk(self, open_positions) -> float:
        """Aggregate worst-case stop risk across all known open positions."""
        if not open_positions:
            return 0.0
        total = 0.0
        positions = open_positions.values() if isinstance(open_positions, dict) else open_positions
        for position in positions:
            risk = self.position_stop_risk(position)
            if risk == float("inf"):
                return risk
            total += risk
        return total

    def proposed_trade_risk(self, entry_price: float, stop_loss: float, qty: int) -> float:
        try:
            qty = int(qty)
            per_share = abs(float(entry_price) - float(stop_loss))
        except (TypeError, ValueError):
            return float("inf")
        if qty <= 0 or per_share <= 0:
            return 0.0
        return per_share * qty

    def realized_loss_used(self) -> float:
        """Amount of the daily loss budget already consumed by net realized P&L."""
        return max(0.0, -float(self.day.realized_pnl))

    def total_risk_if_added(self, open_positions=None, proposed_risk: float = 0.0) -> float:
        """Prospective daily downside = realized loss used + open stop risk + proposed risk."""
        try:
            proposed = max(0.0, float(proposed_risk))
        except (TypeError, ValueError):
            return float("inf")
        open_risk = self.open_stop_risk(open_positions)
        if open_risk == float("inf"):
            return open_risk
        return self.realized_loss_used() + open_risk + proposed

    def can_afford_trade(self, open_positions=None, proposed_risk: float = 0.0) -> bool:
        return self.total_risk_if_added(open_positions, proposed_risk) <= self.max_loss_amount()

    def remaining_daily_risk(self, open_positions=None) -> float:
        used = self.total_risk_if_added(open_positions, 0.0)
        if used == float("inf"):
            return 0.0
        return max(0.0, self.max_loss_amount() - used)

    def can_take_new_trade(
        self,
        current_open_count: int = 0,
        open_positions=None,
        proposed_risk: float = 0.0,
    ) -> bool:
        if self.day.halted:
            return False
        if self.day.trades_taken >= self.cfg.MAX_TRADES_PER_DAY:
            self._halt(f"Max trades per day ({self.cfg.MAX_TRADES_PER_DAY}) reached")
            return False
        max_open = getattr(self.cfg, "MAX_OPEN_POSITIONS", None)
        if max_open is not None and current_open_count >= max_open:
            return False
        if self.day.realized_pnl <= -self.max_loss_amount():
            self._halt(f"Daily loss limit ({self.cfg.MAX_DAILY_LOSS_PCT}% of capital) hit")
            return False
        if not self.can_afford_trade(open_positions, proposed_risk):
            return False
        return True

    def record_trade_result(self, pnl: float):
        self.day.trades_taken += 1
        self.day.realized_pnl += pnl
        if self.day.realized_pnl <= -self.max_loss_amount():
            self._halt(f"Daily loss limit ({self.cfg.MAX_DAILY_LOSS_PCT}% of capital) hit")
        if self.persist:
            _save_day_state(self.day)

    def _halt(self, reason: str):
        self.day.halted = True
        self.day.halt_reason = reason
        if self.persist:
            _save_day_state(self.day)

    def reset_for_new_day(self):
        self.day = DayState()
        if self.persist:
            _save_day_state(self.day)


def circuit_proximity_pct(direction, last_price, lower_limit, upper_limit):
    """Distance (%) from price to the circuit limit relevant to exit liquidity."""
    if last_price is None or last_price <= 0:
        return None
    if direction == "BUY":
        if upper_limit is None or upper_limit <= 0:
            return None
        return (upper_limit - last_price) / last_price * 100
    if lower_limit is None or lower_limit <= 0:
        return None
    return (last_price - lower_limit) / last_price * 100


def is_near_circuit_limit(direction, last_price, lower_limit, upper_limit, threshold_pct):
    """True if within threshold of the relevant circuit limit."""
    distance = circuit_proximity_pct(direction, last_price, lower_limit, upper_limit)
    if distance is None:
        return False
    return distance < threshold_pct
