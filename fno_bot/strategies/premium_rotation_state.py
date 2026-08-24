"""
PREMIUM_ROTATION_SHADOW -- scoped state, opening protection, re-ATM
(sections 13-15).

CRITICAL: this strategy gets its OWN state file path, never
fno_day_state.json. That exact file was the source of a real, live
silent-halt bug hit twice in this project already (2026-08-20 and
again on 2026-08-21's launcher trace) -- two strategies sharing one
state file meant one strategy's halt/trade-count silently blocked
the other. DAY_STATE_PATH below is deliberately distinct.
"""
import json
import os
from dataclasses import dataclass, asdict
from datetime import date as date_cls
from typing import Optional

DAY_STATE_PATH = os.path.join("runtime", "state", "premium_rotation_shadow", "day_state.json")


@dataclass
class DayState:
    date: str
    trades_taken: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""


def load_day_state(path: str, today: str) -> DayState:
    """Loads existing state IF it's for today; otherwise returns a
    fresh state for today. This is the exact behavior that was
    missing/ambiguous in fno_day_state.json -- a stale date must
    result in a reset, not a silently-reused stale halt."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                raw = json.load(f)
            if raw.get("date") == today:
                return DayState(**raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass   # corrupt or malformed file -- fall through to a fresh state, never crash on load
    return DayState(date=today)


def save_day_state(path: str, state: DayState) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp_path, path)   # atomic on POSIX -- never leaves a half-written state file


@dataclass(frozen=True)
class KillSwitchParams:
    max_trades_per_day: int = 3
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 2


def can_take_new_trade(state: DayState, params: KillSwitchParams) -> tuple[bool, str]:
    """Returns (allowed, reason) -- reason is ALWAYS populated, even
    when allowed=True, so a caller logging this every tick never has
    an empty explanation (same invariant as the entry eligibility
    gate)."""
    if state.halted:
        return False, f"already halted: {state.halt_reason}"
    if state.trades_taken >= params.max_trades_per_day:
        return False, f"max trades per day reached ({state.trades_taken}/{params.max_trades_per_day})"
    if state.realized_pnl <= -params.max_daily_loss:
        return False, f"max daily loss reached (₹{state.realized_pnl:.2f} <= -₹{params.max_daily_loss})"
    if state.consecutive_losses >= params.max_consecutive_losses:
        return False, f"max consecutive losses reached ({state.consecutive_losses}/{params.max_consecutive_losses})"
    return True, "within limits"


def record_trade_result(state: DayState, net_pnl: float, params: KillSwitchParams) -> DayState:
    """Updates state after a trade closes. Returns a NEW DayState
    (caller persists it) -- never mutates the daily-loss/halt logic
    silently inside the trade-recording path itself, so the halt
    decision is always explicit and re-checkable via can_take_new_trade()."""
    trades_taken = state.trades_taken + 1
    realized_pnl = state.realized_pnl + net_pnl
    consecutive_losses = state.consecutive_losses + 1 if net_pnl <= 0 else 0

    halted = state.halted
    halt_reason = state.halt_reason
    if trades_taken >= params.max_trades_per_day and not halted:
        halted, halt_reason = True, f"max trades per day reached ({trades_taken})"
    if realized_pnl <= -params.max_daily_loss and not halted:
        halted, halt_reason = True, f"max daily loss reached (₹{realized_pnl:.2f})"
    if consecutive_losses >= params.max_consecutive_losses and not halted:
        halted, halt_reason = True, f"max consecutive losses reached ({consecutive_losses})"

    return DayState(state.date, trades_taken, realized_pnl, consecutive_losses, halted, halt_reason)


# --- opening-market protection (section 14) -------------------------------

def is_within_opening_protection(seconds_since_market_open: float, protection_seconds: float) -> bool:
    """True = still protected, entries must be blocked (but observation/
    logging continues regardless, per spec's explicit instruction to
    collect data during the opening period even when trading is off)."""
    return seconds_since_market_open < protection_seconds


# --- re-ATM logic (section 15) --------------------------------------------

def should_reselect_atm(
    current_underlying: float, selected_strike: float, strike_interval: float,
    reselect_threshold: float, position_open: bool,
) -> bool:
    """NEVER true while a position is open -- managing the instrument
    actually purchased always wins, per spec's explicit instruction.
    Re-ATM is only ever considered while flat."""
    if position_open:
        return False
    return abs(current_underlying - selected_strike) > reselect_threshold
