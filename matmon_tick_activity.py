"""Fail-closed live tick-activity gate for Matmon candidates."""

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class ActivityResult:
    accepted: bool
    reason: str
    valid_updates: int = 0
    latest_age_seconds: float | None = None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _has_valid_depth(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    if not buys or not sells:
        return False

    bid = _number((buys[0] or {}).get("price"))
    ask = _number((sells[0] or {}).get("price"))

    return bid is not None and ask is not None and ask >= bid


def evaluate_tick_activity(
    ws_engine,
    symbol,
    *,
    now=None,
    lookback_seconds=10.0,
    minimum_updates=4,
    maximum_age_seconds=1.0,
):
    now = time.time() if now is None else float(now)

    ticker = (
        getattr(ws_engine, "ws_ticker", None)
        if ws_engine is not None else None
    )
    buffer = (
        getattr(ticker, "tick_buffer", None)
        if ticker is not None else None
    )

    if buffer is None:
        return ActivityResult(False, "MATMON_ACTIVITY_NO_TICK_BUFFER")

    rows = buffer.ticks_received_since(
        symbol,
        now - float(lookback_seconds),
    ) or []

    valid = []
    for tick in rows:
        received_at = _number((tick or {}).get("received_at"))

        if received_at is None or not _has_valid_depth(tick):
            continue

        valid.append((received_at, tick))

    if not valid:
        return ActivityResult(False, "MATMON_ACTIVITY_NO_VALID_DEPTH")

    valid.sort(key=lambda item: item[0])
    latest_age = max(0.0, now - valid[-1][0])

    if latest_age > float(maximum_age_seconds):
        return ActivityResult(
            False,
            "MATMON_ACTIVITY_STALE",
            len(valid),
            latest_age,
        )

    if len(valid) < int(minimum_updates):
        return ActivityResult(
            False,
            "MATMON_ACTIVITY_TOO_FEW_UPDATES",
            len(valid),
            latest_age,
        )

    return ActivityResult(
        True,
        "MATMON_ACTIVITY_CONFIRMED",
        len(valid),
        latest_age,
    )
