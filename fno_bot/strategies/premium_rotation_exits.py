"""Options-selling exit engine.

The SELL_PREMIUM path is deliberately runner-oriented: no fixed profit target,
no early profit clipping, and no aggressive trailing in the first part of a
trade. Risk is controlled by a percentage hard stop, delayed trailing
protection, momentum/underlying invalidation, a long time-stop for stagnant
trades, and the session cutoff.

The structural concepts mirror the user's EL-Bethel approach: protect the
trade only after it has developed, let confirmed direction run, and tighten
only after a meaningful favorable move. Exact thresholds are paper-test
parameters, not claims of optimization.

Exit priority for SELL_PREMIUM is explicit and deterministic:
1. hard stop
2. optional profit target (disabled by default)
3. trailing breach after activation
4. time-stop only when progress remains below the configured minimum
5. momentum/underlying protection while the trade is not yet protected as a runner
6. mandatory session cutoff
If multiple conditions are true on the same tick, the first applicable item wins.
"""
from dataclasses import dataclass
from fno_bot.strategies.premium_rotation import WindowFeatures, RotationParams


@dataclass(frozen=True)
class ExitParams:
    stop_loss_pct: float = 12.0
    profit_target_enabled: bool = False
    profit_target_pct: float = 25.0
    trailing_activation_pct: float = 8.0
    trailing_distance_pct: float = 4.0
    time_stop_seconds: float = 900.0
    time_stop_min_progress_pct: float = 1.0
    momentum_exit_min_profit_pct: float = 3.0
    session_cutoff_hhmm: str = "15:05"

    # Backward-compatible point fields for older callers/tests.
    stop_loss_points: float = 15.0
    profit_target_points: float = 15.0
    trailing_activation_points: float = 20.0
    trailing_distance_points: float = 8.0
    time_stop_min_progress_points: float = 5.0


@dataclass
class OpenPosition:
    direction: str
    entry_price: float
    entry_time: float
    peak_favorable_price: float
    side: str = "LONG"


def _favorable_move(position: OpenPosition, current_price: float) -> float:
    return (
        current_price - position.entry_price
        if position.side == "LONG"
        else position.entry_price - current_price
    )


def _favorable_pct(position: OpenPosition, current_price: float) -> float:
    if position.entry_price <= 0:
        return 0.0
    return _favorable_move(position, current_price) / position.entry_price * 100.0


def check_hard_stop(position, current_price, params):
    move_pct = _favorable_pct(position, current_price)
    if move_pct <= -params.stop_loss_pct:
        return (
            f"HARD_STOP: {position.side} adverse move {abs(move_pct):.2f}% "
            f"reached {params.stop_loss_pct:.2f}%"
        )
    return None


def check_profit_target(position, current_price, params):
    if not params.profit_target_enabled:
        return None
    move_pct = _favorable_pct(position, current_price)
    if move_pct >= params.profit_target_pct:
        return (
            f"PROFIT_TARGET: gain {move_pct:.2f}% reached "
            f"{params.profit_target_pct:.2f}%"
        )
    return None


def check_momentum_reversal(position, features, params_rotation, params_exit=None):
    """Use underlying-confirmed premium reversal as an invalidation signal.

    Once a short trade is already meaningfully profitable, this check does
    not cut the runner. The delayed trailing/structural protection owns that
    phase, which prevents early exits on ordinary option-premium pullbacks.
    """
    if features is None:
        return None

    min_profit = (
        params_exit.momentum_exit_min_profit_pct
        if params_exit is not None else 3.0
    )

    # Caller may attach the latest favorable percentage dynamically.
    current_favorable_pct = getattr(position, "_current_favorable_pct", None)
    if current_favorable_pct is not None and current_favorable_pct >= min_profit:
        return None

    if position.side == "LONG":
        if position.direction == "CE":
            adverse = (
                features.ce_momentum_pct <= params_rotation.pe_weakness_max_pct
                and features.pe_momentum_pct >= params_rotation.ce_momentum_min_pct
                and features.difference_velocity <= -params_rotation.velocity_min
            )
        else:
            adverse = (
                features.pe_momentum_pct <= params_rotation.pe_weakness_max_pct
                and features.ce_momentum_pct >= params_rotation.ce_momentum_min_pct
                and features.difference_velocity >= params_rotation.velocity_min
            )
    else:
        # SELL CE is the bearish thesis; SELL PE is the bullish thesis.
        if position.direction == "CE":
            adverse = (
                features.ce_momentum_pct >= params_rotation.ce_momentum_min_pct
                and features.pe_momentum_pct <= params_rotation.pe_weakness_max_pct
                and features.difference_velocity >= params_rotation.velocity_min
            )
            underlying_reversal = features.underlying_momentum_pct >= 0.10
        else:
            adverse = (
                features.pe_momentum_pct >= params_rotation.ce_momentum_min_pct
                and features.ce_momentum_pct <= params_rotation.pe_weakness_max_pct
                and features.difference_velocity <= -params_rotation.velocity_min
            )
            underlying_reversal = features.underlying_momentum_pct <= -0.10

        adverse = adverse and underlying_reversal

    if adverse:
        return (
            f"MOMENTUM_REVERSAL: confirmed adverse {position.side} "
            f"{position.direction} structure"
        )
    return None


def check_trailing_stop(position, current_price, params):
    peak_pct = _favorable_pct(position, position.peak_favorable_price)
    if peak_pct < params.trailing_activation_pct:
        return None

    if position.side == "SHORT":
        trail_level = position.peak_favorable_price * (
            1.0 + params.trailing_distance_pct / 100.0
        )
        hit = current_price >= trail_level
    else:
        trail_level = position.peak_favorable_price * (
            1.0 - params.trailing_distance_pct / 100.0
        )
        hit = current_price <= trail_level

    if hit:
        return (
            f"TRAILING_STOP: peak favorable {peak_pct:.2f}%, "
            f"price {current_price:.2f} hit {trail_level:.2f}"
        )
    return None


def check_time_stop(position, current_price, now_time, params):
    elapsed = now_time - position.entry_time
    move_pct = _favorable_pct(position, current_price)

    # A profitable runner is never killed merely because time passed.
    if elapsed >= params.time_stop_seconds and move_pct < params.time_stop_min_progress_pct:
        return (
            f"TIME_STOP: {elapsed:.0f}s elapsed, favorable move "
            f"{move_pct:.2f}% below {params.time_stop_min_progress_pct:.2f}%"
        )
    return None


def check_session_cutoff(now_hhmm, params):
    if now_hhmm >= params.session_cutoff_hhmm:
        return (
            f"SESSION_CUTOFF: {now_hhmm} reached mandatory exit "
            f"{params.session_cutoff_hhmm}"
        )
    return None


def evaluate_exit(
    position,
    current_price,
    features,
    params_rotation,
    params_exit,
    now_time,
    now_hhmm,
):
    # Store the current favorable percentage only for this evaluation.
    # OpenPosition remains intentionally simple and serializable.
    current_favorable_pct = _favorable_pct(position, current_price)
    setattr(position, "_current_favorable_pct", current_favorable_pct)

    # Priority is intentional: hard risk protection first, then optional
    # target, then runner protection, then stagnation, then thesis protection,
    # with the mandatory end-of-session exit last.
    checks = [
        lambda: check_hard_stop(position, current_price, params_exit),
        lambda: check_profit_target(position, current_price, params_exit),
        lambda: check_trailing_stop(position, current_price, params_exit),
        lambda: check_time_stop(position, current_price, now_time, params_exit),
        lambda: check_momentum_reversal(
            position, features, params_rotation, params_exit
        ),
        lambda: check_session_cutoff(now_hhmm, params_exit),
    ]
    for check in checks:
        reason = check()
        if reason is not None:
            return reason
    return None
