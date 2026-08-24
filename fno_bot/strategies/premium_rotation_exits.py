"""
PREMIUM_ROTATION_SHADOW -- exit system (section 12, A-F).

Pure decision logic, no I/O. Priority order per spec: hard stop first
(capital protection is the top priority per section 25), then profit
target, momentum reversal, trailing profit, time stop, then forced
end-of-session. evaluate_exit() returns the FIRST applicable reason
in that order -- a position is never checked against a lower-priority
rule if a higher one already fired.
"""
from dataclasses import dataclass
from typing import Optional

from fno_bot.strategies.premium_rotation import WindowFeatures, RotationParams


@dataclass(frozen=True)
class ExitParams:
    """All experimental (section 12B explicitly warns not to assume
    optimality) -- these are starting points for shadow-mode data
    collection, not production defaults."""
    stop_loss_points: float = 15.0          # max loss in premium points before hard exit
    profit_target_points: float = 15.0      # section 12B's example value
    trailing_activation_points: float = 20.0   # profit level that arms the trailing stop
    trailing_distance_points: float = 8.0      # how far behind the peak the trail sits
    time_stop_seconds: float = 90.0         # exit if momentum hasn't materialized by then
    time_stop_min_progress_points: float = 5.0  # "momentum materialized" = at least this much favorable move
    session_cutoff_hhmm: str = "15:15"      # forced flat time, matches spec's EOD requirement


@dataclass
class OpenPosition:
    direction: str          # "CE" or "PE"
    entry_price: float      # premium at entry
    entry_time: float       # monotonic seconds
    peak_favorable_price: float   # best price seen so far in the position's favor, updates externally


def _favorable_move(position: OpenPosition, current_price: float) -> float:
    """Positive = profit direction, negative = loss direction, in
    premium points, regardless of CE/PE (long-premium-only per spec's
    'no naked selling' rule, so both directions are simply 'long')."""
    return current_price - position.entry_price


def check_hard_stop(position: OpenPosition, current_price: float, params: ExitParams) -> Optional[str]:
    move = _favorable_move(position, current_price)
    if move <= -params.stop_loss_points:
        return f"HARD_STOP: loss of {abs(move):.2f} points reached stop of {params.stop_loss_points}"
    return None


def check_profit_target(position: OpenPosition, current_price: float, params: ExitParams) -> Optional[str]:
    move = _favorable_move(position, current_price)
    if move >= params.profit_target_points:
        return f"PROFIT_TARGET: gain of {move:.2f} points reached target of {params.profit_target_points}"
    return None


def check_momentum_reversal(position: OpenPosition, features: WindowFeatures, params_rotation: RotationParams) -> Optional[str]:
    """If we bought CE and CE momentum collapses while PE strengthens
    (or vice versa), exit even if hard SL hasn't been reached --
    this is the mirror-image of the entry condition, checked
    continuously while the position is open."""
    if position.direction == "CE":
        collapsing = features.ce_momentum_pct <= params_rotation.pe_weakness_max_pct
        pe_strengthening = features.pe_momentum_pct >= params_rotation.ce_momentum_min_pct
        velocity_reversed = features.difference_velocity <= -params_rotation.velocity_min
        if collapsing and pe_strengthening and velocity_reversed:
            return (f"MOMENTUM_REVERSAL: CE momentum {features.ce_momentum_pct:.2f}% collapsed, "
                    f"PE strengthening {features.pe_momentum_pct:.2f}%, velocity reversed {features.difference_velocity:.2f}")
    else:
        collapsing = features.pe_momentum_pct <= params_rotation.pe_weakness_max_pct
        ce_strengthening = features.ce_momentum_pct >= params_rotation.ce_momentum_min_pct
        velocity_reversed = features.difference_velocity >= params_rotation.velocity_min
        if collapsing and ce_strengthening and velocity_reversed:
            return (f"MOMENTUM_REVERSAL: PE momentum {features.pe_momentum_pct:.2f}% collapsed, "
                    f"CE strengthening {features.ce_momentum_pct:.2f}%, velocity reversed {features.difference_velocity:.2f}")
    return None


def check_trailing_stop(position: OpenPosition, current_price: float, params: ExitParams) -> Optional[str]:
    """Only active once peak favorable excursion has reached the
    activation threshold. Trail level is peak - trailing_distance,
    NEVER moved backward (position.peak_favorable_price is the
    caller's responsibility to update monotonically -- this function
    only reads it, never mutates it, so the 'never widen the trail'
    guarantee lives in how the caller maintains that field)."""
    peak_move = _favorable_move(position, position.peak_favorable_price)
    if peak_move < params.trailing_activation_points:
        return None   # not armed yet
    trail_level = position.peak_favorable_price - params.trailing_distance_points
    if current_price <= trail_level:
        return (f"TRAILING_STOP: price {current_price:.2f} hit trail level {trail_level:.2f} "
                f"(peak was {position.peak_favorable_price:.2f}, +{peak_move:.2f} points)")
    return None


def check_time_stop(position: OpenPosition, current_price: float, now_time: float, params: ExitParams) -> Optional[str]:
    elapsed = now_time - position.entry_time
    if elapsed < params.time_stop_seconds:
        return None
    move = _favorable_move(position, current_price)
    if move < params.time_stop_min_progress_points:
        return f"TIME_STOP: {elapsed:.1f}s elapsed, only {move:.2f} points progress, below {params.time_stop_min_progress_points}"
    return None


def check_session_cutoff(now_hhmm: str, params: ExitParams) -> Optional[str]:
    if now_hhmm >= params.session_cutoff_hhmm:
        return f"SESSION_CUTOFF: {now_hhmm} reached forced exit time {params.session_cutoff_hhmm}"
    return None


def evaluate_exit(
    position: OpenPosition, current_price: float, features: Optional[WindowFeatures],
    params_rotation: RotationParams, params_exit: ExitParams, now_time: float, now_hhmm: str,
) -> Optional[str]:
    """First applicable reason wins, in priority order A-F. features
    may be None (e.g. insufficient tick history for this window) --
    in that case momentum-reversal simply can't be evaluated this
    tick and is skipped, never treated as 'no reversal happened'."""
    checks = [
        lambda: check_hard_stop(position, current_price, params_exit),
        lambda: check_profit_target(position, current_price, params_exit),
        lambda: check_momentum_reversal(position, features, params_rotation) if features is not None else None,
        lambda: check_trailing_stop(position, current_price, params_exit),
        lambda: check_time_stop(position, current_price, now_time, params_exit),
        lambda: check_session_cutoff(now_hhmm, params_exit),
    ]
    for check in checks:
        reason = check()
        if reason is not None:
            return reason
    return None
