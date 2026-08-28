"""Isolated PAPER/replay experiment for fixed-point option scalping.

This module never places an order.  It replays minute candles after a known
paper-trade entry and answers a narrow question: would a +N point target or
-M point stop have fired first?
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, Mapping, Any


@dataclass(frozen=True)
class FastTargetConfig:
    target_points: float = 15.0
    stop_points: float = 8.0
    conservative_same_candle: bool = True


@dataclass(frozen=True)
class ReplayResult:
    outcome: str  # TARGET | STOP | NO_EXIT
    entry_price: float
    exit_price: float | None
    target_price: float
    stop_price: float
    exit_time: str | None
    points: float | None
    candles_seen: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def replay_long_option(
    entry_price: float,
    candles: Iterable[Mapping[str, Any]],
    cfg: FastTargetConfig | None = None,
) -> ReplayResult:
    """Replay a long CE/PE premium from *entry_price*.

    Candle input needs ``high``, ``low`` and ``date``/``timestamp``.  If both
    target and stop are touched in one minute candle we intentionally assume
    STOP first by default because minute OHLC cannot prove the intrabar order.
    This avoids overstating profitability.
    """
    cfg = cfg or FastTargetConfig()
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if cfg.target_points <= 0 or cfg.stop_points <= 0:
        raise ValueError("target_points and stop_points must be positive")

    target = entry_price + cfg.target_points
    stop = max(0.05, entry_price - cfg.stop_points)
    seen = 0

    for candle in candles:
        seen += 1
        high = float(candle["high"])
        low = float(candle["low"])
        ts = candle.get("date", candle.get("timestamp"))
        ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        hit_target = high >= target
        hit_stop = low <= stop

        if hit_target and hit_stop:
            if cfg.conservative_same_candle:
                return ReplayResult("STOP", entry_price, stop, target, stop,
                                    ts_text, stop - entry_price, seen)
            return ReplayResult("TARGET", entry_price, target, target, stop,
                                ts_text, target - entry_price, seen)
        if hit_stop:
            return ReplayResult("STOP", entry_price, stop, target, stop,
                                ts_text, stop - entry_price, seen)
        if hit_target:
            return ReplayResult("TARGET", entry_price, target, target, stop,
                                ts_text, target - entry_price, seen)

    return ReplayResult("NO_EXIT", entry_price, None, target, stop, None, None, seen)
