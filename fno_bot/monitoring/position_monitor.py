"""
Live position monitoring: MFE/MAE tracking (spec #11, #32) and wiring
the pure exit-hierarchy evaluator (opening_scalper.evaluate_exit_conditions)
to live tick data every cycle, escalating to execution/exit.py the
moment a condition fires.

The excursion math and the per-tick decision are pure functions,
directly unit-testable; only the thin orchestrator at the bottom
(`monitor_step`) touches the tick store / clock / audit log, mirroring
the same pure-core/thin-shell split used throughout this package.
"""
import time
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional, Callable
from zoneinfo import ZoneInfo

from fno_bot.strategies.opening_scalper import evaluate_exit_conditions, ExitCheckResult
from fno_bot.market_data.tick_store import TickStore

logger = logging.getLogger("fno.position_monitor")

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ExcursionState:
    entry_price: float
    max_favorable_price: float
    max_adverse_price: float

    @property
    def mfe_pct(self) -> float:
        return (self.max_favorable_price - self.entry_price) / self.entry_price * 100

    @property
    def mae_pct(self) -> float:
        return (self.max_adverse_price - self.entry_price) / self.entry_price * 100


def init_excursion(entry_price: float) -> ExcursionState:
    return ExcursionState(entry_price, entry_price, entry_price)


def update_excursion(state: ExcursionState, current_price: float) -> ExcursionState:
    """Long-option positions (CE or PE bought) are always favorable-up,
    adverse-down -- there's no short leg in V1 (spec #1: no naked
    selling), so this doesn't need a direction branch."""
    return replace(
        state,
        max_favorable_price=max(state.max_favorable_price, current_price),
        max_adverse_price=min(state.max_adverse_price, current_price),
    )


def is_past_force_square_off(now_ist: datetime, force_square_off_time: str) -> bool:
    """`force_square_off_time` is "HH:MM" in IST, e.g. "15:10"."""
    hh, mm = (int(x) for x in force_square_off_time.split(":"))
    cutoff = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_ist >= cutoff


def compute_monitor_decision(
    *,
    direction: str,
    entry_price: float,
    current_price: float,
    excursion: ExcursionState,
    held_seconds: float,
    signal_still_valid: bool,
    now_ist: datetime,
    cfg,
    emergency_condition: bool = False,
    emergency_reason: Optional[str] = None,
) -> tuple[ExitCheckResult, ExcursionState]:
    """Pure: no I/O. Returns the exit decision plus the updated
    excursion state -- callers persist/act on both."""
    updated = update_excursion(excursion, current_price)
    result = evaluate_exit_conditions(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        target_pct=cfg.TARGET_PCT,
        stop_loss_pct=cfg.STOP_LOSS_PCT,
        held_seconds=held_seconds,
        max_hold_seconds=cfg.MAX_HOLD_SECONDS,
        signal_still_valid=signal_still_valid,
        past_force_square_off=is_past_force_square_off(now_ist, cfg.FORCE_SQUARE_OFF_TIME),
        emergency_condition=emergency_condition,
        emergency_reason=emergency_reason,
    )
    return result, updated


def monitor_step(
    *,
    tick_store: TickStore,
    option_token: int,
    direction: str,
    entry_price: float,
    excursion: ExcursionState,
    entry_monotonic: float,
    signal_still_valid_fn: Callable[[], bool],
    cfg,
    audit_fn: Callable[..., None] = lambda *a, **k: None,
    clock_fn=None,
    now_ist_fn=None,
) -> tuple[ExitCheckResult, ExcursionState, Optional[float]]:
    """
    Thin orchestration for ONE monitoring cycle: reads the latest tick
    for the held option, checks staleness, and returns the exit
    decision + updated excursion state + the price used (None if no
    usable tick was available this cycle -- callers should NOT treat a
    stale/missing tick as any kind of exit signal, just skip the cycle
    and try again next tick).
    """
    clock_fn = clock_fn or time.monotonic
    now_ist_fn = now_ist_fn or (lambda: datetime.now(IST))

    if not tick_store.is_fresh(option_token, cfg.MAX_TICK_AGE_MS):
        audit_fn("MONITOR_STALE_TICK", option_token=option_token)
        return ExitCheckResult(False, None), excursion, None

    tick = tick_store.latest(option_token)
    current_price = tick.last_price
    held_seconds = clock_fn() - entry_monotonic

    result, updated = compute_monitor_decision(
        direction=direction, entry_price=entry_price, current_price=current_price,
        excursion=excursion, held_seconds=held_seconds,
        signal_still_valid=signal_still_valid_fn(), now_ist=now_ist_fn(), cfg=cfg,
    )

    if result.should_exit:
        audit_fn(
            {"HARD_STOP_LOSS": "STOP_TRIGGER", "PROFIT_TARGET": "TARGET_TRIGGER",
             "TIME_STOP": "TIME_STOP_TRIGGER", "SIGNAL_INVALIDATION": "SIGNAL_INVALIDATION"}.get(
                result.reason, result.reason
            ),
            option_token=option_token, current_price=current_price, entry_price=entry_price,
            mfe_pct=updated.mfe_pct, mae_pct=updated.mae_pct, held_seconds=held_seconds,
        )

    return result, updated, current_price
