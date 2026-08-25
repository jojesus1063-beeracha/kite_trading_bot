"""
The opening-momentum options-buying scalper -- the ONLY strategy-
specific module in this codebase (spec #2: everything else in
fno_bot/ is generic infrastructure a future strategy could reuse
unchanged by replacing just this file).

This module owns:
  - turning live ticks into a MarketSnapshot for the signal candidates
  - deciding which (if any) candidate is authorized to trade
  - the exit-condition evaluation (target/stop/time-stop/signal-
    invalidation), per the priority order in config.EXIT_PRIORITY_ORDER
  - shadow/counterfactual capture, run unconditionally every session

It does NOT talk to the broker, WebSocket, or filesystem directly --
those all go through market_data/, execution/, audit/, exactly as
required by the "don't tightly couple strategy to broker execution"
constraint (spec #2).
"""
import logging
from dataclasses import dataclass
from typing import Optional

from fno_bot.strategies.signal_candidates import (
    MarketSnapshot, DirectionSignal, TickPoint, evaluate_all_candidates,
)
from fno_bot.market_data.tick_store import TickStore, NormalizedTick

logger = logging.getLogger("fno.opening_scalper")


def build_snapshot(
    tick_store: TickStore,
    underlying_token: int,
    ce_token: int,
    pe_token: int,
    underlying_prev_close: Optional[float],
    ce_history: tuple = (),
    pe_history: tuple = (),
    underlying_history: tuple = (),
) -> Optional[MarketSnapshot]:
    """
    Reads the latest underlying/CE/PE ticks from the store and
    assembles a MarketSnapshot. Returns None (never a partially-filled/
    garbage snapshot) if ANY of the three legs has no tick yet --
    callers must treat "no snapshot" as "cannot evaluate signals yet",
    not "assume zero" for the missing piece.
    """
    underlying_tick = tick_store.latest(underlying_token)
    ce_tick = tick_store.latest(ce_token)
    pe_tick = tick_store.latest(pe_token)
    if underlying_tick is None or ce_tick is None or pe_tick is None:
        return None

    return MarketSnapshot(
        underlying_price=underlying_tick.last_price,
        underlying_prev_close=underlying_prev_close,
        ce_price=ce_tick.last_price,
        pe_price=pe_tick.last_price,
        ce_best_bid=ce_tick.best_bid, ce_best_ask=ce_tick.best_ask,
        ce_best_bid_qty=ce_tick.best_bid_qty, ce_best_ask_qty=ce_tick.best_ask_qty,
        pe_best_bid=pe_tick.best_bid, pe_best_ask=pe_tick.best_ask,
        pe_best_bid_qty=pe_tick.best_bid_qty, pe_best_ask_qty=pe_tick.best_ask_qty,
        ce_history=ce_history, pe_history=pe_history,
        underlying_history=underlying_history,
    )


def evaluate_signals(snapshot: MarketSnapshot, authorized_signal: Optional[str],
                      candidate_names: list[str] = None) -> tuple[list[DirectionSignal], Optional[DirectionSignal]]:
    """
    Runs every candidate (shadow, always) and separately identifies
    the authorized one's result, if any candidate is authorized at
    all. Returns (all_results, authorized_result_or_None).

    authorized_result is None whenever:
      - authorized_signal is None (nothing authorized yet -- SHADOW-only session), or
      - the authorized candidate itself produced direction=None (no opinion this tick)
    Either way, None here means "do not trade on this tick", full stop.
    """
    all_results = evaluate_all_candidates(snapshot, candidate_names)
    if authorized_signal is None:
        return all_results, None
    authorized_result = next((r for r in all_results if r.candidate == authorized_signal), None)
    if authorized_result is None or authorized_result.direction is None:
        return all_results, None
    return all_results, authorized_result


@dataclass(frozen=True)
class ExitCheckResult:
    should_exit: bool
    reason: Optional[str]   # one of config.EXIT_PRIORITY_ORDER's entries, or None


def evaluate_exit_conditions(
    *,
    direction: str,               # "CE" or "PE" -- which leg is held
    entry_price: float,           # ACTUAL fill price (spec #18 -- never the reference/first-tick price)
    current_price: float,
    target_pct: float,
    stop_loss_pct: float,
    held_seconds: float,
    max_hold_seconds: float,
    signal_still_valid: bool,
    past_force_square_off: bool,
    emergency_condition: bool = False,
    emergency_reason: Optional[str] = None,
) -> ExitCheckResult:
    """
    Evaluates the exit hierarchy in the exact documented priority
    order (spec #15), returning the FIRST condition that fires. All
    percentage math is relative to entry_price -- the actual fill,
    never the pre-entry reference price (spec #18).

    This function is pure (no I/O, no broker calls) so every branch is
    directly unit-testable with synthetic numbers.
    """
    if emergency_condition:
        return ExitCheckResult(True, emergency_reason or "EMERGENCY_RISK_EXIT")

    target_price = entry_price * (1 + target_pct / 100)
    stop_price = entry_price * (1 - stop_loss_pct / 100)

    if current_price <= stop_price:
        return ExitCheckResult(True, "HARD_STOP_LOSS")

    if not signal_still_valid:
        return ExitCheckResult(True, "SIGNAL_INVALIDATION")

    if current_price >= target_price:
        return ExitCheckResult(True, "PROFIT_TARGET")

    if held_seconds >= max_hold_seconds:
        return ExitCheckResult(True, "TIME_STOP")

    if past_force_square_off:
        return ExitCheckResult(True, "END_OF_SESSION_MANDATORY_EXIT")

    return ExitCheckResult(False, None)
