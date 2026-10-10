"""Fail-closed executable-price pullback wait for Matmon LIVE entries."""

from __future__ import annotations

from dataclasses import dataclass
import time

from entry_quality import (
    MAX_ABSOLUTE_SIGNAL_DRIFT_PCT,
    MAX_ADVERSE_LIVE_SLIPPAGE_PCT,
)


@dataclass(frozen=True)
class PullbackWaitDecision:
    accepted: bool
    reason: str
    symbol: str
    direction: str
    signal_price: float
    executable_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None
    quote_age_seconds: float | None = None
    elapsed_seconds: float = 0.0
    samples: int = 0

    def to_dict(self):
        return dict(self.__dict__)


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def is_adverse_slippage_only(validation) -> bool:
    """True only for the ordinary 0.15% adverse-slippage rejection."""
    adverse = _number(getattr(validation, "adverse_slippage_pct", None))
    drift = getattr(validation, "drift_pct", None)
    try:
        drift = float(drift)
    except (TypeError, ValueError):
        return False
    return bool(
        not getattr(validation, "accepted", False)
        and adverse is not None
        and adverse > MAX_ADVERSE_LIVE_SLIPPAGE_PCT
        and abs(drift) <= MAX_ABSOLUTE_SIGNAL_DRIFT_PCT
        and str(getattr(validation, "reason", "")).startswith(
            "adverse entry slippage exceeds"
        )
    )


def _full_quote(tick):
    depth = (tick or {}).get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if len(buys) < 5 or len(sells) < 5:
        return None
    received_at = _number((tick or {}).get("received_at"))
    bid = _number((buys[0] or {}).get("price"))
    ask = _number((sells[0] or {}).get("price"))
    if received_at is None or bid is None or ask is None or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return received_at, bid, ask, (ask - bid) / mid * 10_000.0


def wait_for_pullback(
    ws_engine,
    symbol,
    direction,
    signal_price,
    *,
    timeout_seconds=30.0,
    entry_band_pct=0.10,
    reversal_pct=0.15,
    maximum_age_seconds=1.0,
    maximum_spread_multiplier=1.5,
    spread_allowance_bps=1.0,
    poll_seconds=0.05,
    trading_window_fn=None,
    risk_fn=None,
    now_fn=time.time,
    sleep_fn=time.sleep,
):
    """Wait for a fresh executable quote to pull back, without authorizing entry.

    Acceptance only permits the normal Matmon confirmation stack to restart.
    """
    direction = str(direction).upper()
    signal_price = _number(signal_price)
    ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine else None
    buffer = getattr(ticker, "tick_buffer", None) if ticker else None
    if direction not in {"BUY", "SELL"} or signal_price is None or buffer is None:
        return PullbackWaitDecision(
            False, "MATMON_PULLBACK_UNAVAILABLE", str(symbol), direction,
            signal_price or 0.0,
        )

    started_at = float(now_fn())
    deadline = started_at + float(timeout_seconds)
    samples = 0
    baseline_spread = None
    last = None
    last_reason = "MATMON_PULLBACK_TIMEOUT"

    while float(now_fn()) <= deadline:
        if trading_window_fn is not None and not trading_window_fn():
            last_reason = "MATMON_PULLBACK_TRADING_WINDOW_CLOSED"
            break
        if risk_fn is not None and not risk_fn():
            last_reason = "MATMON_PULLBACK_RISK_BLOCKED"
            break

        quote = _full_quote(buffer.latest(symbol))
        now = float(now_fn())
        if quote is not None:
            received_at, bid, ask, spread_bps = quote
            age = max(0.0, now - received_at)
            last = (bid, ask, spread_bps, age)
            # A pre-wait quote cannot satisfy the pullback condition.
            if received_at >= started_at and age <= float(maximum_age_seconds):
                samples += 1
                if baseline_spread is None:
                    baseline_spread = spread_bps
                max_spread = max(
                    baseline_spread * float(maximum_spread_multiplier),
                    baseline_spread + float(spread_allowance_bps),
                )
                executable = ask if direction == "BUY" else bid
                if direction == "BUY":
                    reversed = executable < signal_price * (
                        1.0 - float(reversal_pct) / 100.0
                    )
                    in_band = (
                        executable >= signal_price * (1.0 - float(reversal_pct) / 100.0)
                        and executable <= signal_price * (1.0 + float(entry_band_pct) / 100.0)
                    )
                else:
                    reversed = executable > signal_price * (1.0 + float(reversal_pct) / 100.0)
                    in_band = (
                        executable <= signal_price * (1.0 + float(reversal_pct) / 100.0)
                        and executable >= signal_price * (1.0 - float(entry_band_pct) / 100.0)
                    )
                if reversed:
                    last_reason = "MATMON_PULLBACK_REVERSED"
                    break
                if in_band and spread_bps <= max_spread:
                    return PullbackWaitDecision(
                        True, "MATMON_PULLBACK_PRICE_RECOVERED", str(symbol),
                        direction, signal_price, executable, bid, ask,
                        spread_bps, age, max(0.0, now - started_at), samples,
                    )
                if in_band:
                    last_reason = "MATMON_PULLBACK_SPREAD_UNSTABLE"
        sleep_fn(float(poll_seconds))

    bid = ask = spread = age = None
    if last is not None:
        bid, ask, spread, age = last
    return PullbackWaitDecision(
        False, last_reason, str(symbol), direction, signal_price,
        (ask if direction == "BUY" else bid), bid, ask, spread, age,
        max(0.0, float(now_fn()) - started_at), samples,
    )
