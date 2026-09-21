"""Premium-rotation exit decisions, supporting long-premium and short-premium paper positions."""
from dataclasses import dataclass
from typing import Optional
from fno_bot.strategies.premium_rotation import WindowFeatures, RotationParams

@dataclass(frozen=True)
class ExitParams:
    stop_loss_points: float = 15.0
    profit_target_points: float = 15.0
    trailing_activation_points: float = 20.0
    trailing_distance_points: float = 8.0
    time_stop_seconds: float = 90.0
    time_stop_min_progress_points: float = 5.0
    session_cutoff_hhmm: str = "15:15"

@dataclass
class OpenPosition:
    direction: str                 # CE or PE
    entry_price: float             # premium at entry
    entry_time: float
    peak_favorable_price: float
    side: str = "LONG"             # LONG = buy premium; SHORT = sell premium

def _favorable_move(position: OpenPosition, current_price: float) -> float:
    return (current_price - position.entry_price
            if position.side == "LONG"
            else position.entry_price - current_price)

def check_hard_stop(position, current_price, params):
    move = _favorable_move(position, current_price)
    if move <= -params.stop_loss_points:
        return f"HARD_STOP: loss of {abs(move):.2f} points reached stop of {params.stop_loss_points}"
    return None

def check_profit_target(position, current_price, params):
    move = _favorable_move(position, current_price)
    if move >= params.profit_target_points:
        return f"PROFIT_TARGET: gain of {move:.2f} points reached target of {params.profit_target_points}"
    return None

def check_momentum_reversal(position, features, params_rotation):
    if features is None:
        return None
    if position.side == "LONG":
        if position.direction == "CE":
            adverse = (features.ce_momentum_pct <= params_rotation.pe_weakness_max_pct
                       and features.pe_momentum_pct >= params_rotation.ce_momentum_min_pct
                       and features.difference_velocity <= -params_rotation.velocity_min)
        else:
            adverse = (features.pe_momentum_pct <= params_rotation.pe_weakness_max_pct
                       and features.ce_momentum_pct >= params_rotation.ce_momentum_min_pct
                       and features.difference_velocity >= params_rotation.velocity_min)
    else:
        # A short premium is hurt when the sold leg strengthens.
        if position.direction == "CE":
            adverse = (features.ce_momentum_pct >= params_rotation.ce_momentum_min_pct
                       and features.pe_momentum_pct <= params_rotation.pe_weakness_max_pct
                       and features.difference_velocity >= params_rotation.velocity_min)
        else:
            adverse = (features.pe_momentum_pct >= params_rotation.ce_momentum_min_pct
                       and features.ce_momentum_pct <= params_rotation.pe_weakness_max_pct
                       and features.difference_velocity <= -params_rotation.velocity_min)
    if adverse:
        return f"MOMENTUM_REVERSAL: adverse {position.side} {position.direction} structure detected"
    return None

def check_trailing_stop(position, current_price, params):
    peak_move = _favorable_move(position, position.peak_favorable_price)
    if peak_move < params.trailing_activation_points:
        return None
    trail_level = (position.peak_favorable_price - params.trailing_distance_points
                   if position.side == "LONG"
                   else position.peak_favorable_price + params.trailing_distance_points)
    hit = current_price <= trail_level if position.side == "LONG" else current_price >= trail_level
    if hit:
        return f"TRAILING_STOP: price {current_price:.2f} hit trail level {trail_level:.2f}"
    return None

def check_time_stop(position, current_price, now_time, params):
    elapsed = now_time - position.entry_time
    move = _favorable_move(position, current_price)
    if elapsed >= params.time_stop_seconds and move < params.time_stop_min_progress_points:
        return f"TIME_STOP: {elapsed:.1f}s elapsed, only {move:.2f} points progress, below {params.time_stop_min_progress_points}"
    return None

def check_session_cutoff(now_hhmm, params):
    if now_hhmm >= params.session_cutoff_hhmm:
        return f"SESSION_CUTOFF: {now_hhmm} reached forced exit time {params.session_cutoff_hhmm}"
    return None

def evaluate_exit(position, current_price, features, params_rotation, params_exit, now_time, now_hhmm):
    checks = [
        lambda: check_hard_stop(position, current_price, params_exit),
        lambda: check_profit_target(position, current_price, params_exit),
        lambda: check_momentum_reversal(position, features, params_rotation),
        lambda: check_trailing_stop(position, current_price, params_exit),
        lambda: check_time_stop(position, current_price, now_time, params_exit),
        lambda: check_session_cutoff(now_hhmm, params_exit),
    ]
    for check in checks:
        reason = check()
        if reason is not None:
            return reason
    return None
