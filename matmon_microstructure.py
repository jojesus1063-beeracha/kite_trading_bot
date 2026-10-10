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
    microprice_change: float | None = None
    confirmation_path: str | None = None

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


def _flat_metrics(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if len(buys) < 5 or len(sells) < 5:
        return None
    bid = _number(buys[0].get("price"))
    ask = _number(sells[0].get("price"))
    bid_qty = _number(buys[0].get("quantity"))
    ask_qty = _number(sells[0].get("quantity"))
    ltp = _number((tick or {}).get("last_price"))
    if ltp is None:
        ltp = _number((tick or {}).get("ltp"))
    received = _number((tick or {}).get("received_at"))
    w5 = weighted_5_imbalance(tick)
    if None in (bid, ask, bid_qty, ask_qty, ltp, received, w5):
        return None
    if bid <= 0 or ask < bid or bid_qty < 0 or ask_qty < 0 or received <= 0:
        return None
    denominator = bid_qty + ask_qty
    mid = (bid + ask) / 2.0
    if denominator <= 0 or mid <= 0:
        return None
    microprice = (ask * bid_qty + bid * ask_qty) / denominator
    spread_bps = (ask - bid) / mid * 10_000.0
    return received, bid, ask, ltp, w5, microprice, spread_bps


def evaluate_flat_with_pressure(
    direction,
    ticks,
    *,
    minimum_imbalance=0.30,
    maximum_spread_multiplier=1.50,
    spread_allowance_bps=1.0,
):
    """Confirm an exactly-flat quote path only with independent book pressure."""
    if direction not in {"BUY", "SELL"}:
        return MicrostructureEvidence(False, False, "MATMON_INVALID_DIRECTION", direction)

    rows = []
    for tick in ticks or ():
        row = _flat_metrics(tick)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row[0])
    if len(rows) < 3:
        return MicrostructureEvidence(
            False, False, "MATMON_FLAT_PRESSURE_INSUFFICIENT", direction,
            sample_count=len(rows), confirmation_path="FLAT_WITH_PRESSURE",
        )
    rows = rows[:3]
    first, last = rows[0], rows[-1]
    quotes_flat = all(row[1] == first[1] and row[2] == first[2] for row in rows)
    spread_limit = max(
        first[6] * float(maximum_spread_multiplier),
        first[6] + float(spread_allowance_bps),
    )
    spread_stable = all(row[6] <= spread_limit for row in rows)
    ltp_change = last[3] - first[3]
    w5_change = last[4] - first[4]
    microprice_change = last[5] - first[5]
    threshold = abs(float(minimum_imbalance))

    if direction == "BUY":
        pressure_ok = (
            last[4] >= threshold and w5_change > 0
            and microprice_change > 0 and ltp_change >= 0
        )
    else:
        pressure_ok = (
            last[4] <= -threshold and w5_change < 0
            and microprice_change < 0 and ltp_change <= 0
        )
    accepted = bool(quotes_flat and spread_stable and pressure_ok)
    return MicrostructureEvidence(
        True,
        accepted,
        "MATMON_FLAT_PRESSURE_CONFIRMED" if accepted else "MATMON_FLAT_PRESSURE_REJECT",
        direction,
        ltp_change / (last[0] - first[0]) if last[0] > first[0] else None,
        first[4],
        last[4],
        w5_change,
        len(rows),
        microprice_change,
        "FLAT_WITH_PRESSURE",
    )


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
