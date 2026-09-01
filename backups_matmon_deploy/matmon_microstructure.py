#!/usr/bin/env python3
"""Matmon microstructure hard gate computed from the same fresh CLEAN window."""
from dataclasses import dataclass, asdict
from math import isfinite

LEVEL_WEIGHTS = (5.0, 4.0, 3.0, 2.0, 1.0)


@dataclass
class MicrostructureEvidence:
    available: bool
    accepted: bool
    reason: str
    direction: str | None = None
    ltp_velocity_per_sec: float | None = None
    first_weighted_5_imbalance: float | None = None
    last_weighted_5_imbalance: float | None = None
    weighted_5_imbalance_change: float | None = None
    sample_count: int = 0

    def to_dict(self):
        return asdict(self)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def weighted_5_imbalance(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if len(buys) < 5 or len(sells) < 5:
        return None

    bid_total = 0.0
    ask_total = 0.0
    for i, weight in enumerate(LEVEL_WEIGHTS):
        try:
            bid_qty = _number(buys[i].get("quantity"))
            ask_qty = _number(sells[i].get("quantity"))
        except (AttributeError, IndexError):
            return None
        if bid_qty is None or ask_qty is None or bid_qty < 0 or ask_qty < 0:
            return None
        bid_total += weight * bid_qty
        ask_total += weight * ask_qty

    denominator = bid_total + ask_total
    if denominator <= 0:
        return None
    return (bid_total - ask_total) / denominator


def evaluate_microstructure(direction, ticks):
    if direction not in {"BUY", "SELL"}:
        return MicrostructureEvidence(False, False, "MATMON_INVALID_DIRECTION", direction)

    valid_ltp = []
    valid_w5 = []
    for tick in ticks or ():
        ts = _number((tick or {}).get("received_at"))
        ltp = _number((tick or {}).get("last_price"))
        if ltp is None:
            ltp = _number((tick or {}).get("ltp"))
        w5 = weighted_5_imbalance(tick)
        if ts is not None and ts > 0 and ltp is not None:
            valid_ltp.append((ts, ltp))
        if ts is not None and ts > 0 and w5 is not None:
            valid_w5.append((ts, w5))

    if len(valid_ltp) < 2 or len(valid_w5) < 2:
        return MicrostructureEvidence(
            False, False, "MATMON_MICROSTRUCTURE_INSUFFICIENT", direction,
            sample_count=min(len(valid_ltp), len(valid_w5)),
        )

    valid_ltp.sort(key=lambda row: row[0])
    valid_w5.sort(key=lambda row: row[0])
    elapsed = valid_ltp[-1][0] - valid_ltp[0][0]
    if elapsed <= 0:
        return MicrostructureEvidence(False, False, "MATMON_INVALID_LTP_WINDOW", direction)

    velocity = (valid_ltp[-1][1] - valid_ltp[0][1]) / elapsed
    first_w5 = valid_w5[0][1]
    last_w5 = valid_w5[-1][1]
    change = last_w5 - first_w5

    if direction == "BUY":
        accepted = velocity > 0 and last_w5 > 0 and change > 0
    else:
        accepted = velocity < 0 and last_w5 < 0 and change < 0

    return MicrostructureEvidence(
        True,
        accepted,
        "MATMON_MICROSTRUCTURE_CONFIRMED" if accepted else "MATMON_MICROSTRUCTURE_REJECT",
        direction,
        velocity,
        first_w5,
        last_w5,
        change,
        min(len(valid_ltp), len(valid_w5)),
    )
