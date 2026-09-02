"""Fixed stop and target calculations based on confirmed broker fills."""

from __future__ import annotations

import math
from typing import Tuple


def fixed_levels_from_fill(
    direction: str,
    fill_price: float,
    stop_loss_percent: float,
    profit_target_percent: float,
) -> Tuple[float, float]:
    """
    Calculate stop and target from the actual confirmed fill price.

    BUY:
        stop below fill and target above fill.

    SELL:
        stop above fill and target below fill.
    """
    direction = str(direction).upper().strip()
    fill_price = float(fill_price)
    stop_loss_percent = float(stop_loss_percent)
    profit_target_percent = float(profit_target_percent)

    if direction not in {"BUY", "SELL"}:
        raise ValueError(
            f"Unsupported trade direction: {direction!r}"
        )

    values = (
        fill_price,
        stop_loss_percent,
        profit_target_percent,
    )

    if not all(math.isfinite(value) for value in values):
        raise ValueError("Trade-level inputs must be finite")

    if fill_price <= 0:
        raise ValueError("Fill price must be greater than zero")

    if stop_loss_percent <= 0:
        raise ValueError(
            "Stop-loss percentage must be greater than zero"
        )

    if profit_target_percent <= 0:
        raise ValueError(
            "Profit-target percentage must be greater than zero"
        )

    stop_fraction = stop_loss_percent / 100.0
    target_fraction = profit_target_percent / 100.0

    if direction == "BUY":
        stop_price = fill_price * (1.0 - stop_fraction)
        target_price = fill_price * (1.0 + target_fraction)
    else:
        stop_price = fill_price * (1.0 + stop_fraction)
        target_price = fill_price * (1.0 - target_fraction)

    return stop_price, target_price


def reward_risk_ratio(
    stop_loss_percent: float,
    profit_target_percent: float,
) -> float:
    stop_loss_percent = float(stop_loss_percent)
    profit_target_percent = float(profit_target_percent)

    if stop_loss_percent <= 0:
        raise ValueError(
            "Stop-loss percentage must be greater than zero"
        )

    return profit_target_percent / stop_loss_percent
