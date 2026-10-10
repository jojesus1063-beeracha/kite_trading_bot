"""Active-signal-only ranking for simultaneous Matmon candidates.

The score changes processing priority only.  It never authorizes a signal,
changes its direction, or bypasses quote/microstructure/risk guards.
"""

from __future__ import annotations

import math


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def active_signal_score(signal) -> float:
    """Rank stronger EMA separation and DI agreement ahead of weaker ones."""

    detail = getattr(signal, "price_action_detail", None) or {}
    values = detail.get("matmon") or {}
    ema_fast = _finite(values.get("ema3"))
    ema_slow = _finite(values.get("ema15"))
    plus_di = _finite(values.get("plus_di"))
    minus_di = _finite(values.get("minus_di"))

    if None in (ema_fast, ema_slow, plus_di, minus_di):
        return 0.0

    mid = (abs(ema_fast) + abs(ema_slow)) / 2.0
    ema_separation_bps = (
        abs(ema_fast - ema_slow) / mid * 10_000.0
        if mid > 0
        else 0.0
    )
    di_margin = abs(plus_di - minus_di)

    # Both components are direction-neutral magnitudes because direction has
    # already been authorized by EMA ordering plus DI agreement.
    return round(ema_separation_bps + di_margin, 6)
