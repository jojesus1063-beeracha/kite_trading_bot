"""Hybrid scalp/runner exit state for paper and live positions.

The feature deliberately reuses the existing verified exit path.  This
module only calculates durable position metadata and advances that metadata
after confirmed fills; it never submits orders itself.
"""

from __future__ import annotations

import math


SCALP_PENDING = "SCALP_PENDING"
RUNNER_PENDING = "RUNNER_PENDING"


def configure_hybrid_exit(position: dict, cfg) -> dict:
    """Attach a 1R scalp / 2R runner plan to a new position.

    Positions with fewer than two shares cannot be split and retain the
    ordinary fixed target. Live order/stop coordination is implemented by
    main.py; this pure helper never performs broker side effects.

    Recovery may deliberately produce an unresolved position with ``entry``
    and ``stop`` set to ``None`` while manual reconciliation is required.
    Hybrid planning must fail closed for that state rather than raising during
    restart recovery.
    """

    enabled = bool(getattr(cfg, "ENABLE_HYBRID_EXIT", False))
    fixed_target = bool(getattr(cfg, "ENABLE_FIXED_TARGET", False))
    quantity = int(position.get("qty") or 0)

    position["hybrid_exit_enabled"] = False

    # Confirmed triple-pattern trades use the separately validated fixed
    # target (1% top / 2% bottom); the generic 1R/2R hybrid would replace it.
    if position.get("exit_policy") == "PATTERN_FIXED":
        return position

    if not enabled or not fixed_target or quantity < 2:
        return position

    # Fail closed on unresolved or malformed recovery levels.  Do not invent a
    # fill/stop price: the caller's manual-reconciliation state must remain the
    # authority until real broker data is available.
    try:
        entry = float(position.get("entry"))
        stop = float(position.get("stop"))
    except (TypeError, ValueError):
        return position

    direction = str(position.get("direction") or "").upper()
    risk_per_share = abs(entry - stop)
    scalp_r = float(getattr(cfg, "HYBRID_SCALP_R", 1.0))
    runner_r = float(getattr(cfg, "HYBRID_RUNNER_R", 2.0))
    fraction = float(getattr(cfg, "HYBRID_SCALP_FRACTION", 0.5))

    values = (entry, stop, risk_per_share, scalp_r, runner_r, fraction)
    if (
        direction not in {"BUY", "SELL"}
        or not all(math.isfinite(value) for value in values)
        or entry <= 0
        or stop <= 0
        or risk_per_share <= 0
        or scalp_r <= 0
        or runner_r <= scalp_r
        or not 0 < fraction < 1
    ):
        return position

    scalp_quantity = int(math.floor(quantity * fraction))
    scalp_quantity = min(max(scalp_quantity, 1), quantity - 1)
    runner_quantity = quantity - scalp_quantity
    sign = 1.0 if direction == "BUY" else -1.0
    scalp_target = entry + sign * risk_per_share * scalp_r
    runner_target = entry + sign * risk_per_share * runner_r

    position.update({
        "hybrid_exit_enabled": True,
        "hybrid_exit_stage": SCALP_PENDING,
        "hybrid_initial_quantity": quantity,
        "hybrid_scalp_quantity": scalp_quantity,
        "hybrid_scalp_remaining_quantity": scalp_quantity,
        "hybrid_runner_quantity": runner_quantity,
        "hybrid_scalp_target": scalp_target,
        "hybrid_runner_target": runner_target,
        "hybrid_original_stop": stop,
        "hybrid_move_stop_to_breakeven": bool(
            getattr(cfg, "HYBRID_MOVE_STOP_TO_BREAKEVEN", True)
        ),
        "target": scalp_target,
    })
    return position


def requested_exit_quantity(position: dict, reason: str) -> int:
    """Return the intended quantity for the current exit trigger."""

    current = int(position.get("qty") or 0)
    if (
        position.get("hybrid_exit_enabled")
        and reason == "hybrid_scalp_1r"
        and position.get("hybrid_exit_stage") == SCALP_PENDING
    ):
        remaining = int(
            position.get("hybrid_scalp_remaining_quantity") or 0
        )
        return min(current, max(remaining, 0))
    return current


def apply_confirmed_hybrid_fill(
    position: dict,
    *,
    reason: str,
    confirmed_quantity: int,
    exit_price: float,
    net_pnl: float,
) -> None:
    """Advance the persisted hybrid stage after a confirmed scalp fill."""

    if (
        not position.get("hybrid_exit_enabled")
        or reason != "hybrid_scalp_1r"
        or position.get("hybrid_exit_stage") != SCALP_PENDING
    ):
        return

    outstanding = int(
        position.get("hybrid_scalp_remaining_quantity") or 0
    )
    outstanding = max(0, outstanding - int(confirmed_quantity))
    position["hybrid_scalp_remaining_quantity"] = outstanding
    position["hybrid_last_scalp_exit_price"] = float(exit_price)
    position["hybrid_realized_scalp_pnl"] = float(
        position.get("hybrid_realized_scalp_pnl") or 0.0
    ) + float(net_pnl)

    if outstanding > 0:
        return

    position["hybrid_exit_stage"] = RUNNER_PENDING
    position["target"] = float(position["hybrid_runner_target"])
    if position.get("hybrid_move_stop_to_breakeven", True):
        position["stop"] = float(position["entry"])
