"""
Daily kill-switches for the F&O bot (spec #24): MAX_DAILY_LOSS,
MAX_TRADES_PER_DAY, MAX_CONSECUTIVE_LOSSES. Persisted per trading day,
completely separate file from the equity bot's day_state.json (see
architecture review Section B) -- a loss on one bot must never halt
the other.

Once a hard limit fires, NEW entries are blocked for the rest of the
session, but any already-open position must continue to be actively
managed (target/stop/time-stop/exit) -- can_take_new_trade() governs
entries only; it is never consulted by exit logic.
"""
import json
import os
from datetime import date
from dataclasses import dataclass

DAY_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_day_state.json")


@dataclass
class FnoDayState:
    trades_taken: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""


def _save(state: FnoDayState, path=None):
    p = path or DAY_STATE_PATH
    data = {
        "date": date.today().isoformat(),
        "trades_taken": state.trades_taken,
        "realized_pnl": state.realized_pnl,
        "consecutive_losses": state.consecutive_losses,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
    }
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def _load(path=None) -> FnoDayState:
    p = path or DAY_STATE_PATH
    if not os.path.exists(p):
        return FnoDayState()
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return FnoDayState()
    if data.get("date") != date.today().isoformat():
        return FnoDayState()  # previous day's state -- start fresh, never carry over
    return FnoDayState(
        trades_taken=data.get("trades_taken", 0),
        realized_pnl=data.get("realized_pnl", 0.0),
        consecutive_losses=data.get("consecutive_losses", 0),
        halted=data.get("halted", False),
        halt_reason=data.get("halt_reason", ""),
    )


class FnoKillSwitch:
    def __init__(self, cfg, persist: bool = True, path=None):
        self.cfg = cfg
        self.persist = persist
        self.path = path
        self.day = _load(path) if persist else FnoDayState()

    def can_take_new_trade(self) -> bool:
        """Governs new ENTRIES only -- never call this to decide
        whether to manage/exit an already-open position."""
        if self.day.halted:
            return False
        if self.day.trades_taken >= self.cfg.MAX_TRADES_PER_DAY:
            self._halt(f"Max trades per day ({self.cfg.MAX_TRADES_PER_DAY}) reached")
            return False
        if self.day.realized_pnl <= -abs(self.cfg.MAX_DAILY_LOSS):
            self._halt(f"Daily loss limit (Rs{self.cfg.MAX_DAILY_LOSS}) hit")
            return False
        if self.day.consecutive_losses >= self.cfg.MAX_CONSECUTIVE_LOSSES:
            self._halt(f"Max consecutive losses ({self.cfg.MAX_CONSECUTIVE_LOSSES}) reached")
            return False
        return True

    def record_trade_result(self, net_pnl: float):
        self.day.trades_taken += 1
        self.day.realized_pnl += net_pnl
        if net_pnl < 0:
            self.day.consecutive_losses += 1
        else:
            self.day.consecutive_losses = 0

        if self.day.realized_pnl <= -abs(self.cfg.MAX_DAILY_LOSS):
            self._halt(f"Daily loss limit (Rs{self.cfg.MAX_DAILY_LOSS}) hit")
        elif self.day.consecutive_losses >= self.cfg.MAX_CONSECUTIVE_LOSSES:
            self._halt(f"Max consecutive losses ({self.cfg.MAX_CONSECUTIVE_LOSSES}) reached")

        if self.persist:
            _save(self.day, self.path)

    def _halt(self, reason: str):
        if not self.day.halted:
            self.day.halted = True
            self.day.halt_reason = reason
            if self.persist:
                _save(self.day, self.path)

    def reset_for_new_day(self):
        self.day = FnoDayState()
        if self.persist:
            _save(self.day, self.path)
