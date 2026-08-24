"""Five-level market-depth confirmation for live entries.

Depth is deliberately a veto/confirmation layer only.  It can allow an
existing EMA/policy direction or skip it; it never creates or reverses a
BUY/SELL signal.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class DepthConfirmation:
    accepted: bool
    classification: str
    reason: str
    sample_count: int = 0
    coverage_seconds: float = 0.0
    latest_age_seconds: Optional[float] = None
    median_imbalance: Optional[float] = None
    buy_pressure_fraction: Optional[float] = None
    sell_pressure_fraction: Optional[float] = None
    latest_buy_quantity: Optional[int] = None
    latest_sell_quantity: Optional[int] = None
    latest_spread_bps: Optional[float] = None
    executable_depth: Optional[int] = None
    required_executable_depth: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _quantity(levels: Iterable[dict]) -> int:
    total = 0
    for level in list(levels or [])[:5]:
        try:
            value = int(level.get("quantity") or 0)
        except (TypeError, ValueError):
            value = 0
        total += max(0, value)
    return total


def _snapshot(tick: dict) -> Optional[dict]:
    depth = tick.get("depth") or {}
    buys = list(depth.get("buy") or [])[:5]
    sells = list(depth.get("sell") or [])[:5]
    if not buys or not sells:
        return None

    buy_quantity = _quantity(buys)
    sell_quantity = _quantity(sells)
    total = buy_quantity + sell_quantity
    if total <= 0:
        return None

    try:
        best_bid = float(buys[0].get("price") or 0.0)
        best_ask = float(sells[0].get("price") or 0.0)
    except (TypeError, ValueError):
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid) * 10_000.0

    try:
        received_at = float(tick.get("received_at"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(received_at):
        return None

    return {
        "received_at": received_at,
        "buy_quantity": buy_quantity,
        "sell_quantity": sell_quantity,
        "imbalance": (buy_quantity - sell_quantity) / total,
        "spread_bps": spread_bps,
    }


def evaluate_depth_ticks(
    ticks: Iterable[dict],
    direction: str,
    planned_quantity: int,
    *,
    now: Optional[float] = None,
    window_seconds: float = 30.0,
    min_coverage_seconds: float = 15.0,
    min_samples: int = 5,
    imbalance_threshold: float = 0.20,
    persistence_fraction: float = 0.70,
    max_age_seconds: float = 2.0,
    max_spread_bps: float = 5.0,
    executable_depth_multiple: float = 2.0,
) -> DepthConfirmation:
    """Evaluate recent full-depth ticks for an already-selected direction."""
    direction = str(direction or "").upper()
    if direction not in {"BUY", "SELL"}:
        return DepthConfirmation(False, "INVALID", "invalid final direction")

    now = time.time() if now is None else float(now)
    cutoff = now - float(window_seconds)
    snapshots = []
    for tick in ticks or []:
        snap = _snapshot(tick)
        if snap is not None and cutoff <= snap["received_at"] <= now + 1.0:
            snapshots.append(snap)
    snapshots.sort(key=lambda item: item["received_at"])

    # One observation per wall-clock second prevents a burst of ticks in one
    # instant from falsely satisfying the persistence rule.
    per_second = {}
    for snap in snapshots:
        per_second[int(snap["received_at"])] = snap
    samples = [per_second[key] for key in sorted(per_second)]
    if not samples:
        return DepthConfirmation(False, "UNAVAILABLE", "no valid five-level depth samples")

    latest = samples[-1]
    latest_age = max(0.0, now - latest["received_at"])
    coverage = max(0.0, latest["received_at"] - samples[0]["received_at"])
    common = {
        "sample_count": len(samples),
        "coverage_seconds": round(coverage, 3),
        "latest_age_seconds": round(latest_age, 3),
        "latest_buy_quantity": latest["buy_quantity"],
        "latest_sell_quantity": latest["sell_quantity"],
        "latest_spread_bps": round(latest["spread_bps"], 4),
    }

    if latest_age > max_age_seconds:
        return DepthConfirmation(
            False, "STALE", f"latest depth is {latest_age:.2f}s old", **common
        )
    if len(samples) < int(min_samples) or coverage < min_coverage_seconds:
        return DepthConfirmation(
            False,
            "INSUFFICIENT_HISTORY",
            "depth window lacks the required samples or time coverage",
            **common,
        )
    if latest["spread_bps"] > max_spread_bps:
        return DepthConfirmation(
            False,
            "WIDE_SPREAD",
            f"spread {latest['spread_bps']:.2f} bps exceeds {max_spread_bps:.2f} bps",
            **common,
        )

    planned_quantity = max(0, int(planned_quantity or 0))
    required_depth = int(math.ceil(planned_quantity * executable_depth_multiple))
    executable_depth = (
        latest["sell_quantity"] if direction == "BUY" else latest["buy_quantity"]
    )
    common.update(
        {
            "executable_depth": executable_depth,
            "required_executable_depth": required_depth,
        }
    )
    if planned_quantity <= 0:
        return DepthConfirmation(False, "INVALID", "planned quantity is zero", **common)
    if executable_depth < required_depth:
        return DepthConfirmation(
            False,
            "INSUFFICIENT_DEPTH",
            f"executable depth {executable_depth} is below required {required_depth}",
            **common,
        )

    imbalances = [sample["imbalance"] for sample in samples]
    median_imbalance = float(statistics.median(imbalances))
    buy_fraction = sum(value >= imbalance_threshold for value in imbalances) / len(imbalances)
    sell_fraction = sum(value <= -imbalance_threshold for value in imbalances) / len(imbalances)
    common.update(
        {
            "median_imbalance": round(median_imbalance, 6),
            "buy_pressure_fraction": round(buy_fraction, 6),
            "sell_pressure_fraction": round(sell_fraction, 6),
        }
    )

    persistent_buy = (
        median_imbalance >= imbalance_threshold
        and buy_fraction >= persistence_fraction
    )
    persistent_sell = (
        median_imbalance <= -imbalance_threshold
        and sell_fraction >= persistence_fraction
    )
    opposes = (direction == "BUY" and persistent_sell) or (
        direction == "SELL" and persistent_buy
    )
    supports = (direction == "BUY" and persistent_buy) or (
        direction == "SELL" and persistent_sell
    )

    if opposes:
        return DepthConfirmation(
            False,
            "OPPOSING",
            f"persistent five-level pressure opposes {direction}",
            **common,
        )
    if supports:
        return DepthConfirmation(
            True,
            "CONFIRMED",
            f"persistent five-level pressure confirms {direction}",
            **common,
        )
    return DepthConfirmation(
        True,
        "NEUTRAL",
        "five-level pressure is mixed; existing direction remains unchanged",
        **common,
    )


def evaluate_live_depth(
    ws_engine,
    symbol: str,
    direction: str,
    planned_quantity: int,
    cfg,
    *,
    now: Optional[float] = None,
) -> DepthConfirmation:
    """Read buffered WebSocket ticks and apply configured live thresholds."""
    if not bool(getattr(cfg, "ENABLE_DEPTH_CONFIRMATION_GATE", False)):
        return DepthConfirmation(True, "DISABLED", "depth confirmation gate disabled")
    ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine is not None else None
    buffer = getattr(ticker, "tick_buffer", None) if ticker is not None else None
    if buffer is None:
        return DepthConfirmation(False, "UNAVAILABLE", "live WebSocket depth buffer unavailable")

    now = time.time() if now is None else float(now)
    window = float(getattr(cfg, "DEPTH_CONFIRMATION_WINDOW_SECONDS", 30.0))
    ticks = buffer.ticks_received_since(symbol, now - window)
    return evaluate_depth_ticks(
        ticks,
        direction,
        planned_quantity,
        now=now,
        window_seconds=window,
        min_coverage_seconds=float(getattr(cfg, "DEPTH_CONFIRMATION_MIN_COVERAGE_SECONDS", 15.0)),
        min_samples=int(getattr(cfg, "DEPTH_CONFIRMATION_MIN_SAMPLES", 5)),
        imbalance_threshold=float(getattr(cfg, "DEPTH_CONFIRMATION_IMBALANCE", 0.20)),
        persistence_fraction=float(getattr(cfg, "DEPTH_CONFIRMATION_PERSISTENCE", 0.70)),
        max_age_seconds=float(getattr(cfg, "DEPTH_CONFIRMATION_MAX_AGE_SECONDS", 2.0)),
        max_spread_bps=float(getattr(cfg, "DEPTH_CONFIRMATION_MAX_SPREAD_BPS", 5.0)),
        executable_depth_multiple=float(getattr(cfg, "DEPTH_CONFIRMATION_SIZE_MULTIPLE", 2.0)),
    )
