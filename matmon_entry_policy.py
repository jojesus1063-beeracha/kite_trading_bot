#!/usr/bin/env python3
"""Matmon HaElohim entry-direction policy.

Strategy authorization stages handled here:
1. EMA3/EMA15 direction on completed 3-minute candles.
2. DI14 must agree with that direction.

Quote CLEAN and microstructure confirmation are handled by dedicated modules.
This module does not place orders.
"""
from dataclasses import dataclass, asdict
from math import isfinite
from typing import Optional


@dataclass
class MatmonDirectionDecision:
    accepted: bool
    direction: Optional[str]
    reason: str
    ema3: Optional[float] = None
    ema15: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None

    def to_dict(self):
        return asdict(self)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def ema_direction(ema3, ema15):
    fast = _finite(ema3)
    slow = _finite(ema15)
    if fast is None or slow is None:
        return None
    if fast > slow:
        return "BUY"
    if fast < slow:
        return "SELL"
    return None


def di_agrees(direction, plus_di, minus_di):
    pdi = _finite(plus_di)
    mdi = _finite(minus_di)
    if direction not in {"BUY", "SELL"} or pdi is None or mdi is None:
        return False
    if direction == "BUY":
        return pdi > mdi
    return mdi > pdi


def evaluate_direction(*, ema3, ema15, plus_di, minus_di):
    direction = ema_direction(ema3, ema15)
    if direction is None:
        return MatmonDirectionDecision(
            False, None, "EMA_DIRECTION_UNAVAILABLE", ema3, ema15, plus_di, minus_di
        )

    if not di_agrees(direction, plus_di, minus_di):
        return MatmonDirectionDecision(
            False, direction, "DI_DISAGREES", ema3, ema15, plus_di, minus_di
        )

    return MatmonDirectionDecision(
        True, direction, "MATMON_EMA_DI_CONFIRMED", ema3, ema15, plus_di, minus_di
    )
