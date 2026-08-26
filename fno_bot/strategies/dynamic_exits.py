"""Immutable entry-time exit plans for long option positions."""
from dataclasses import dataclass

from fno_bot.reporting.costs import estimate_trade_cost


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class DynamicExitPlan:
    stop_pct: float
    target_pct: float
    breakeven_trigger_pct: float
    breakeven_floor_pct: float
    trailing_activation_pct: float
    trailing_distance_pct: float
    observed_range_pct: float
    entry_momentum_pct: float
    entry_spread_pct: float


def build_dynamic_exit_plan(prices, momentum_pct, spread_pct, entry_price, quantity):
    clean = [float(price) for price in prices if price is not None and float(price) > 0]
    if len(clean) < 2 or entry_price <= 0 or quantity <= 0:
        raise ValueError("dynamic exit plan requires valid history, fill price, and quantity")
    range_pct = (max(clean) - min(clean)) / clean[-1] * 100
    stop_pct = _clamp(1.25 * range_pct + max(float(spread_pct or 0), 0), 3.0, 7.5)
    target_pct = _clamp(max(1.8 * stop_pct, 2.0 * abs(float(momentum_pct or 0))), 6.0, 15.0)
    same_price_cost = estimate_trade_cost(quantity * entry_price, quantity * entry_price)
    cost_floor_pct = same_price_cost / (quantity * entry_price) * 100
    return DynamicExitPlan(
        stop_pct=stop_pct,
        target_pct=target_pct,
        breakeven_trigger_pct=stop_pct,
        breakeven_floor_pct=cost_floor_pct,
        trailing_activation_pct=1.5 * stop_pct,
        trailing_distance_pct=0.75 * stop_pct,
        observed_range_pct=range_pct,
        entry_momentum_pct=float(momentum_pct or 0),
        entry_spread_pct=float(spread_pct or 0),
    )

