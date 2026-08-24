"""
Risk management: position sizing and the daily kill-switch.

This module tracks state for the trading day (trade count, realized
P&L) and decides whether a new signal is allowed to be traded.
"""

import json
import os
import re
from datetime import date
from dataclasses import dataclass, field

DAY_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "day_state.json")


def _save_day_state(day: "DayState"):
    data = {
        "date": date.today().isoformat(),
        "trades_taken": day.trades_taken,
        "realized_pnl": day.realized_pnl,
        "consecutive_losses": day.consecutive_losses,
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
        return DayState()  # saved state is from a previous day; start fresh
    return DayState(
        trades_taken=data.get("trades_taken", 0),
        realized_pnl=data.get("realized_pnl", 0.0),
        consecutive_losses=data.get("consecutive_losses", 0),
        halted=data.get("halted", False),
        halt_reason=data.get("halt_reason", ""),
    )


@dataclass
class DayState:
    trades_taken: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg, persist: bool = True):
        """
        persist=True  (default, used by main.py / live trading): loads
            and saves day_state.json as before -- unchanged behavior.
        persist=False (used by backtest.py): starts with a fresh
            in-memory-only DayState and NEVER reads or writes
            day_state.json, so backtests can never inherit or corrupt
            the live bot's real daily risk state.
        """
        self.cfg = cfg
        self.persist = persist
        self.day = _load_day_state() if persist else DayState()
        self._reconcile_obsolete_trade_cap_halt()

    def _reconcile_obsolete_trade_cap_halt(self) -> None:
        """Clear only a stale trade-count halt after the configured cap rises.

        A max-trades halt records the threshold in its reason.  If a later
        PAPER session deliberately raises that threshold, retaining the old
        halt would incorrectly block every scan.  Daily-loss and all unknown
        halt reasons remain sticky and are never changed here.
        """
        if not self.day.halted:
            return

        match = re.fullmatch(
            r"Max trades per day \((\d+)\) reached",
            str(self.day.halt_reason or "").strip(),
        )
        if match is None:
            return

        previous_limit = int(match.group(1))
        current_limit = int(self.cfg.MAX_TRADES_PER_DAY)
        trades_taken = int(self.day.trades_taken)
        if current_limit <= previous_limit or trades_taken >= current_limit:
            return

        self.day.halted = False
        self.day.halt_reason = ""
        if self.persist:
            _save_day_state(self.day)

    def max_loss_amount(self) -> float:
        return self.cfg.CAPITAL * self.cfg.MAX_DAILY_LOSS_PCT / 100

    def risk_amount_per_trade(self) -> float:
        return self.cfg.CAPITAL * self.cfg.RISK_PER_TRADE_PCT / 100

    def position_size(self, entry_price: float, stop_loss: float) -> int:
        """
        Quantity such that (entry - stop) * qty ~= risk_amount_per_trade.

        NOTE: MAX_POSITION_SIZE_PCT is enforced separately, in
        executor.cap_quantity_by_margin(), against the REAL margin
        required (via Kite's order_margins()), not notional value here.
        Notional value ignores MIS leverage and is a poor proxy for
        actual capital usage -- a margin-based cap is more accurate.
        """
        per_share_risk = abs(entry_price - stop_loss)
        if per_share_risk <= 0:
            return 0
        qty = int(self.risk_amount_per_trade() / per_share_risk)
        return max(qty, 0)

    def can_take_new_trade(self, current_open_count: int = 0) -> bool:
        if self.day.halted:
            return False
        if self.day.trades_taken >= self.cfg.MAX_TRADES_PER_DAY:
            self._halt(f"Max trades per day ({self.cfg.MAX_TRADES_PER_DAY}) reached")
            return False
        max_open = getattr(self.cfg, "MAX_OPEN_POSITIONS", None)
        if max_open is not None and current_open_count >= max_open:
            return False  # simultaneous-position cap -- just skip this cycle, not a halt
        if (
            bool(getattr(self.cfg, "DAILY_LOSS_KILL_SWITCH_ENABLED", True))
            and self.day.realized_pnl <= -self.max_loss_amount()
        ):
            self._halt(f"Daily loss limit ({self.cfg.MAX_DAILY_LOSS_PCT}% of capital) hit")
            return False
        return True

    def record_trade_result(self, pnl: float):
        self.day.trades_taken += 1
        self.day.realized_pnl += pnl
        if pnl < 0:
            self.day.consecutive_losses += 1
        else:
            self.day.consecutive_losses = 0
        consecutive_limit = int(getattr(self.cfg, "MAX_CONSECUTIVE_LOSSES", 0) or 0)
        if consecutive_limit > 0 and self.day.consecutive_losses >= consecutive_limit:
            self._halt(f"Max consecutive losses ({consecutive_limit}) reached")
        elif (
            bool(getattr(self.cfg, "DAILY_LOSS_KILL_SWITCH_ENABLED", True))
            and self.day.realized_pnl <= -self.max_loss_amount()
        ):
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
        _save_day_state(self.day)


def circuit_proximity_pct(direction, last_price, lower_limit, upper_limit):
    """
    Distance (as %) from current price to the circuit limit that would
    matter for this trade's exit: upper limit for BUY (can't sell into
    a locked-up circuit going further up), lower limit for SELL
    (downside exit liquidity dries up near the lower circuit).
    Returns None if circuit data is missing/invalid.
    """
    if last_price is None or last_price <= 0:
        return None
    if direction == "BUY":
        if upper_limit is None or upper_limit <= 0:
            return None
        return (upper_limit - last_price) / last_price * 100
    else:
        if lower_limit is None or lower_limit <= 0:
            return None
        return (last_price - lower_limit) / last_price * 100


def is_near_circuit_limit(direction, last_price, lower_limit, upper_limit, threshold_pct):
    """True if within `threshold_pct` of the relevant circuit limit --
    fails safe to False (don't block) if circuit data is unavailable,
    since blocking on missing data would be a worse failure mode than
    proceeding without this specific check."""
    distance = circuit_proximity_pct(direction, last_price, lower_limit, upper_limit)
    if distance is None:
        return False
    return distance < threshold_pct
