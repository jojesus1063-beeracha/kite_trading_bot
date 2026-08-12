"""PAPER/replay-only delayed candlestick confirmation engine.

This module is intentionally isolated from live execution. It converts closed-candle
price-action patterns into pending setups, waits for confirmation, and creates a
risk plan. It does not submit broker orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import math

import numpy as np
import pandas as pd

VOLUME_LOOKBACK = 20
EMA_PERIOD = 50
DEFAULT_TICK_SIZE = 0.05
DEFAULT_RISK_PCT = 0.20
MIN_RR = 2.0
MAX_WAIT_BARS = 2  # 2 completed 3m candles = max 6-minute wait

MARUBOZU_MAX_WICK_BODY_RATIO = 0.10
HAMMER_MIN_LOWER_WICK_BODY_RATIO = 2.0
HAMMER_MAX_LOWER_WICK_BODY_RATIO = 3.5
HAMMER_MAX_UPPER_WICK_BODY_RATIO = 0.40
DOJI_MAX_BODY_RANGE_RATIO = 0.10
STRONG_CONFIRMATION_BODY_RANGE_RATIO = 0.60


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Pattern(str, Enum):
    BULLISH_MARUBOZU = "BULLISH_MARUBOZU"
    HAMMER = "HAMMER"
    BULLISH_ENGULFING = "BULLISH_ENGULFING"
    INSIDE_BAR = "INSIDE_BAR"
    DOJI = "DOJI"


@dataclass(frozen=True)
class PendingSetup:
    pattern: Pattern
    setup_index: int
    pattern_high: float
    pattern_low: float
    trigger_price: Optional[float]
    stop_price: Optional[float]
    direction: Optional[Direction]


@dataclass(frozen=True)
class TradePlan:
    pattern: Pattern
    direction: Direction
    setup_index: int
    entry_index: int
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    risk_per_share: float
    total_risk: float
    rr: float


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    out = df.copy()
    out["ema50"] = out["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    out["volume_sma20"] = out["volume"].rolling(VOLUME_LOOKBACK).mean()
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = typical * out["volume"]

    if "date" in out.columns:
        session = pd.to_datetime(out["date"]).dt.date
        cum_pv = pv.groupby(session).cumsum()
        cum_vol = out["volume"].groupby(session).cumsum()
    else:
        cum_pv = pv.cumsum()
        cum_vol = out["volume"].cumsum()

    out["vwap"] = cum_pv / cum_vol.replace(0, np.nan)
    return out


def _body(c):
    return abs(float(c["close"]) - float(c["open"]))


def _range(c):
    return max(float(c["high"]) - float(c["low"]), 0.0)


def _upper_wick(c):
    return float(c["high"]) - max(float(c["open"]), float(c["close"]))


def _lower_wick(c):
    return min(float(c["open"]), float(c["close"])) - float(c["low"])


def _bull(c):
    return float(c["close"]) > float(c["open"])


def _bear(c):
    return float(c["close"]) < float(c["open"])


def is_bullish_marubozu(c) -> bool:
    body = _body(c)
    return bool(
        _bull(c)
        and body > 0
        and _upper_wick(c) <= body * MARUBOZU_MAX_WICK_BODY_RATIO
        and _lower_wick(c) <= body * MARUBOZU_MAX_WICK_BODY_RATIO
    )


def is_hammer(c) -> bool:
    body = _body(c)
    if body <= 0:
        return False
    lower = _lower_wick(c)
    upper = _upper_wick(c)
    return bool(
        lower >= body * HAMMER_MIN_LOWER_WICK_BODY_RATIO
        and lower <= body * HAMMER_MAX_LOWER_WICK_BODY_RATIO
        and upper <= body * HAMMER_MAX_UPPER_WICK_BODY_RATIO
    )


def is_bullish_engulfing(prev, cur) -> bool:
    return bool(
        _bear(prev)
        and _bull(cur)
        and float(cur["open"]) <= float(prev["close"])
        and float(cur["close"]) >= float(prev["open"])
    )


def is_inside_bar(mother, inside) -> bool:
    return bool(
        float(inside["high"]) < float(mother["high"])
        and float(inside["low"]) > float(mother["low"])
    )


def is_doji(c) -> bool:
    rng = _range(c)
    return bool(rng > 0 and (_body(c) / rng) <= DOJI_MAX_BODY_RANGE_RATIO)


def volume_passes(c) -> bool:
    avg = c.get("volume_sma20")
    return bool(avg is not None and not pd.isna(avg) and float(c["volume"]) > float(avg))


def long_context_passes(c) -> bool:
    close = float(c["close"])
    vwap = c.get("vwap")
    ema = c.get("ema50")
    return bool(
        (vwap is not None and not pd.isna(vwap) and close > float(vwap))
        or (ema is not None and not pd.isna(ema) and close > float(ema))
    )


def short_context_passes(c) -> bool:
    close = float(c["close"])
    vwap = c.get("vwap")
    ema = c.get("ema50")
    return bool(
        (vwap is not None and not pd.isna(vwap) and close < float(vwap))
        or (ema is not None and not pd.isna(ema) and close < float(ema))
    )


def strong_bullish_close(c) -> bool:
    rng = _range(c)
    return bool(_bull(c) and rng > 0 and (_body(c) / rng) >= STRONG_CONFIRMATION_BODY_RANGE_RATIO)


def strong_bearish_close(c) -> bool:
    rng = _range(c)
    return bool(_bear(c) and rng > 0 and (_body(c) / rng) >= STRONG_CONFIRMATION_BODY_RANGE_RATIO)


def position_size(account_equity: float, entry: float, stop: float, risk_pct: float = DEFAULT_RISK_PCT) -> int:
    per_share = abs(entry - stop)
    if account_equity <= 0 or per_share <= 0 or risk_pct <= 0:
        return 0
    max_risk = account_equity * (risk_pct / 100.0)
    return max(math.floor(max_risk / per_share), 0)


def build_plan(
    pattern: Pattern,
    direction: Direction,
    setup_index: int,
    entry_index: int,
    entry: float,
    stop: float,
    account_equity: float,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> Optional[TradePlan]:
    if direction == Direction.BUY and stop >= entry:
        return None
    if direction == Direction.SELL and stop <= entry:
        return None

    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return None

    # SL is already derived from the closed pattern geometry above.
    # TP is hard-set at 2R from the confirmed entry.
    if direction == Direction.BUY:
        target = entry + MIN_RR * risk_per_share
    else:
        target = entry - MIN_RR * risk_per_share

    qty = position_size(account_equity, entry, stop, risk_pct)
    if qty <= 0:
        return None

    return TradePlan(
        pattern=pattern,
        direction=direction,
        setup_index=setup_index,
        entry_index=entry_index,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=qty,
        risk_per_share=risk_per_share,
        total_risk=risk_per_share * qty,
        rr=MIN_RR,
    )


def detect_setup(df: pd.DataFrame, i: int, tick_size: float = DEFAULT_TICK_SIZE) -> Optional[PendingSetup]:
    """Detect a setup from fully closed candle i. No forming candle may be passed here."""
    if i < 1:
        return None
    cur = df.iloc[i]
    prev = df.iloc[i - 1]

    if is_bullish_marubozu(cur) and volume_passes(cur) and long_context_passes(cur):
        return PendingSetup(Pattern.BULLISH_MARUBOZU, i, float(cur.high), float(cur.low), float(cur.high) + tick_size, float(cur.low) - tick_size, Direction.BUY)

    if is_hammer(cur) and volume_passes(cur) and long_context_passes(cur):
        return PendingSetup(Pattern.HAMMER, i, float(cur.high), float(cur.low), float(cur.high) + tick_size, float(cur.low) - tick_size, Direction.BUY)

    if is_bullish_engulfing(prev, cur) and volume_passes(cur) and long_context_passes(cur):
        low = min(float(prev.low), float(cur.low))
        high = max(float(prev.high), float(cur.high))
        return PendingSetup(Pattern.BULLISH_ENGULFING, i, high, low, None, low - tick_size, Direction.BUY)

    if is_inside_bar(prev, cur) and volume_passes(cur) and long_context_passes(cur):
        return PendingSetup(Pattern.INSIDE_BAR, i, float(prev.high), float(prev.low), float(prev.high) + tick_size, float(cur.low) - tick_size, Direction.BUY)

    if is_doji(cur) and volume_passes(cur):
        return PendingSetup(Pattern.DOJI, i, float(cur.high), float(cur.low), None, None, None)

    return None


def confirm_setup(
    df: pd.DataFrame,
    setup: PendingSetup,
    i: int,
    account_equity: float,
    tick_size: float = DEFAULT_TICK_SIZE,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> Optional[TradePlan]:
    """Confirm a pending setup using a later fully closed candle, except engulfing next-bar-open rule."""
    if i <= setup.setup_index or i - setup.setup_index > MAX_WAIT_BARS:
        return None
    cur = df.iloc[i]

    if setup.pattern in {Pattern.BULLISH_MARUBOZU, Pattern.HAMMER, Pattern.INSIDE_BAR}:
        # Confirmation requires a CLOSED candle to actually close above the trigger,
        # not merely touch it intrabar. This is intentionally stricter than a raw stop-order touch.
        if float(cur.close) <= float(setup.trigger_price):
            return None
        return build_plan(setup.pattern, Direction.BUY, setup.setup_index, i, float(setup.trigger_price), float(setup.stop_price), account_equity, risk_pct)

    if setup.pattern == Pattern.BULLISH_ENGULFING:
        # Entry is the next candle open, after the engulfing candle has fully closed.
        if i != setup.setup_index + 1:
            return None
        return build_plan(setup.pattern, Direction.BUY, setup.setup_index, i, float(cur.open), float(setup.stop_price), account_equity, risk_pct)

    if setup.pattern == Pattern.DOJI:
        if float(cur.close) > setup.pattern_high and strong_bullish_close(cur) and long_context_passes(cur):
            stop = setup.pattern_low - tick_size
            return build_plan(Pattern.DOJI, Direction.BUY, setup.setup_index, i, float(cur.close), stop, account_equity, risk_pct)
        if float(cur.close) < setup.pattern_low and strong_bearish_close(cur) and short_context_passes(cur):
            stop = setup.pattern_high + tick_size
            return build_plan(Pattern.DOJI, Direction.SELL, setup.setup_index, i, float(cur.close), stop, account_equity, risk_pct)

    return None


def replay_candidates(
    raw_df: pd.DataFrame,
    account_equity: float = 5000.0,
    tick_size: float = DEFAULT_TICK_SIZE,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> list[TradePlan]:
    """Closed-candle candidate replay only; no broker calls, persistence, or execution side effects."""
    df = add_indicators(raw_df)
    pending: list[PendingSetup] = []
    plans: list[TradePlan] = []

    for i in range(VOLUME_LOOKBACK, len(df)):
        next_pending: list[PendingSetup] = []
        for setup in pending:
            plan = confirm_setup(df, setup, i, account_equity, tick_size, risk_pct)
            if plan is not None:
                plans.append(plan)
            elif i - setup.setup_index <= MAX_WAIT_BARS:
                next_pending.append(setup)
        pending = next_pending

        new_setup = detect_setup(df, i, tick_size)
        if new_setup is not None:
            pending.append(new_setup)

    return plans
