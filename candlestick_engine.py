"""Comprehensive closed-candle candlestick entry-timing engine.

This is a PURE DECISION MODULE. It never submits broker orders.
It is designed to sit downstream of the existing technical/ADX/RSI/PA/MA
pipeline and receive only the final intended BUY/SELL direction.

Global rules:
- closed candles only (caller must pass completed bars only)
- pattern-forming candle volume > prior 20-bar SMA volume
- BUY context: close > session VWAP AND close > EMA50
- SELL context: close < session VWAP AND close < EMA50
- minimum 2R target
- PAPER sizing default = 0.20% equity risk per trade
- pending breakout/next-open setups expire after max_wait_bars
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import math
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("candlestick_engine")


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Trigger(str, Enum):
    CLOSED_CANDLE = "CLOSED_CANDLE"
    NEXT_OPEN = "NEXT_OPEN"
    BREAKOUT = "BREAKOUT"


class GateState(str, Enum):
    NO_PATTERN = "NO_PATTERN"
    WAITING = "WAITING"
    CONFIRMED = "CONFIRMED"


class Pattern(str, Enum):
    BULLISH_MARUBOZU = "BULLISH_MARUBOZU"
    BEARISH_MARUBOZU = "BEARISH_MARUBOZU"
    HAMMER = "HAMMER"
    INVERTED_HAMMER = "INVERTED_HAMMER"
    SHOOTING_STAR = "SHOOTING_STAR"
    HANGING_MAN = "HANGING_MAN"
    DOJI = "DOJI"
    DRAGONFLY_DOJI = "DRAGONFLY_DOJI"
    GRAVESTONE_DOJI = "GRAVESTONE_DOJI"
    BULLISH_ENGULFING = "BULLISH_ENGULFING"
    BEARISH_ENGULFING = "BEARISH_ENGULFING"
    PIERCING_LINE = "PIERCING_LINE"
    DARK_CLOUD_COVER = "DARK_CLOUD_COVER"
    BULLISH_HARAMI = "BULLISH_HARAMI"
    BEARISH_HARAMI = "BEARISH_HARAMI"
    TWEEZER_BOTTOM = "TWEEZER_BOTTOM"
    TWEEZER_TOP = "TWEEZER_TOP"
    BULLISH_KICKER = "BULLISH_KICKER"
    BEARISH_KICKER = "BEARISH_KICKER"
    MORNING_STAR = "MORNING_STAR"
    EVENING_STAR = "EVENING_STAR"
    THREE_WHITE_SOLDIERS = "THREE_WHITE_SOLDIERS"
    THREE_BLACK_CROWS = "THREE_BLACK_CROWS"
    THREE_INSIDE_UP = "THREE_INSIDE_UP"
    THREE_INSIDE_DOWN = "THREE_INSIDE_DOWN"
    RISING_THREE_METHODS = "RISING_THREE_METHODS"
    FALLING_THREE_METHODS = "FALLING_THREE_METHODS"
    INSIDE_BAR_SQUEEZE = "INSIDE_BAR_SQUEEZE"


@dataclass(frozen=True)
class EngineConfig:
    risk_pct: float = 0.20
    min_rr: float = 2.0
    volume_lookback: int = 20
    body_lookback: int = 20
    ema_period: int = 50
    marubozu_body_multiple: float = 1.5
    marubozu_max_wick_range_ratio: float = 0.05
    doji_max_body_range_ratio: float = 0.10
    long_wick_body_multiple: float = 2.0
    hammer_max_upper_wick_range_ratio: float = 0.15
    tweezer_tolerance_pct: float = 0.05
    max_wait_bars: int = 2
    min_large_body_multiple: float = 1.25
    star_max_body_ratio_to_first: float = 0.50
    close_near_extreme_ratio: float = 0.25


@dataclass(frozen=True)
class Setup:
    pattern: Pattern
    side: Side
    trigger: Trigger
    setup_index: int
    pattern_indices: tuple[int, ...]
    trigger_price: Optional[float]
    stop_price: float
    reason: str


@dataclass(frozen=True)
class TradePlan:
    pattern: Pattern
    side: Side
    trigger: Trigger
    setup_index: int
    entry_index: int
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    risk_per_share: float
    planned_risk: float
    rr: float
    reason: str


@dataclass(frozen=True)
class EntryGateResult:
    state: GateState
    intended_direction: str
    reason: str
    pattern: Optional[Pattern] = None
    setup: Optional[Setup] = None
    plan: Optional[TradePlan] = None


@dataclass
class CandlestickEngine:
    config: EngineConfig = field(default_factory=EngineConfig)
    pending: dict[str, list[Setup]] = field(default_factory=dict)


# ----------------------------- indicators -----------------------------

def add_indicators(df: pd.DataFrame, cfg: EngineConfig = EngineConfig()) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
    out = df.copy()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["ema50"] = out["close"].ewm(span=cfg.ema_period, adjust=False).mean()
    out["volume_sma20"] = out["volume"].rolling(cfg.volume_lookback).mean().shift(1)
    out["body"] = (out["close"] - out["open"]).abs()
    out["avg_body20"] = out["body"].rolling(cfg.body_lookback).mean().shift(1)
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = typical * out["volume"]
    if "date" in out.columns:
        session = pd.to_datetime(out["date"]).dt.date
        out["vwap"] = pv.groupby(session).cumsum() / out["volume"].groupby(session).cumsum().replace(0, np.nan)
    else:
        out["vwap"] = pv.cumsum() / out["volume"].cumsum().replace(0, np.nan)
    return out


# ----------------------------- candle geometry -----------------------------

def body(c): return abs(float(c.close) - float(c.open))
def rng(c): return max(float(c.high) - float(c.low), 0.0)
def upper_wick(c): return float(c.high) - max(float(c.open), float(c.close))
def lower_wick(c): return min(float(c.open), float(c.close)) - float(c.low)
def bull(c): return float(c.close) > float(c.open)
def bear(c): return float(c.close) < float(c.open)
def body_high(c): return max(float(c.open), float(c.close))
def body_low(c): return min(float(c.open), float(c.close))


def volume_ok(c) -> bool:
    vma = c.get("volume_sma20")
    return bool(vma is not None and not pd.isna(vma) and float(c.volume) > float(vma))


def all_volume_ok(df: pd.DataFrame, indices: Iterable[int]) -> bool:
    return all(volume_ok(df.iloc[j]) for j in indices)


def context_ok(c, side: Side) -> bool:
    vwap = c.get("vwap")
    ema = c.get("ema50")
    if vwap is None or ema is None or pd.isna(vwap) or pd.isna(ema):
        return False
    close = float(c.close)
    if side == Side.BUY:
        return close > float(vwap) and close > float(ema)
    return close < float(vwap) and close < float(ema)


def large_body(c, multiple: float = 1.25) -> bool:
    avg = c.get("avg_body20")
    return bool(avg is not None and not pd.isna(avg) and body(c) > multiple * float(avg))


# ----------------------------- pattern detectors -----------------------------

def is_bullish_marubozu(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(bull(c) and r > 0 and large_body(c, cfg.marubozu_body_multiple)
                and upper_wick(c) < cfg.marubozu_max_wick_range_ratio * r
                and lower_wick(c) < cfg.marubozu_max_wick_range_ratio * r)


def is_bearish_marubozu(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(bear(c) and r > 0 and large_body(c, cfg.marubozu_body_multiple)
                and upper_wick(c) < cfg.marubozu_max_wick_range_ratio * r
                and lower_wick(c) < cfg.marubozu_max_wick_range_ratio * r)


def is_hammer(c, cfg=EngineConfig()):
    """Accept green OR red demand-rejection hammers.

    Required:
    - lower wick >= 2x body
    - body in top 30% of full range
    - small upper wick: <=15% of range OR <=1x body
    """
    r, b = rng(c), body(c)
    if r <= 0 or b <= 0:
        return False
    lower = lower_wick(c)
    upper = upper_wick(c)
    body_position = (body_low(c) - float(c.low)) / r
    return bool(
        lower >= cfg.long_wick_body_multiple * b
        and body_position >= 0.70
        and (upper <= cfg.hammer_max_upper_wick_range_ratio * r or upper <= b)
    )


def is_inverted_hammer(c, cfg=EngineConfig()):
    r, b = rng(c), body(c)
    return bool(r > 0 and b > 0 and upper_wick(c) >= cfg.long_wick_body_multiple * b
                and body_high(c) <= float(c.low) + 0.30 * r)


def is_shooting_star(c, cfg=EngineConfig()):
    r, b = rng(c), body(c)
    return bool(r > 0 and b > 0 and upper_wick(c) >= cfg.long_wick_body_multiple * b
                and body_high(c) <= float(c.low) + 0.30 * r)


def is_hanging_man(c, cfg=EngineConfig()):
    r, b = rng(c), body(c)
    return bool(r > 0 and b > 0 and lower_wick(c) >= cfg.long_wick_body_multiple * b
                and body_low(c) >= float(c.low) + 0.70 * r)


def is_doji(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(r > 0 and body(c) < cfg.doji_max_body_range_ratio * r)


def is_dragonfly_doji(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(is_doji(c, cfg) and r > 0 and upper_wick(c) <= 0.10 * r and lower_wick(c) >= 0.60 * r)


def is_gravestone_doji(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(is_doji(c, cfg) and r > 0 and lower_wick(c) <= 0.10 * r and upper_wick(c) >= 0.60 * r)


def is_bullish_engulfing(a, b):
    return bear(a) and bull(b) and body_low(b) <= body_low(a) and body_high(b) >= body_high(a)


def is_bearish_engulfing(a, b):
    return bull(a) and bear(b) and body_low(b) <= body_low(a) and body_high(b) >= body_high(a)


def is_piercing_line(a, b):
    mid = (float(a.open) + float(a.close)) / 2
    return bear(a) and bull(b) and float(b.open) < float(a.low) and float(b.close) > mid and float(b.close) < float(a.open)


def is_dark_cloud_cover(a, b):
    mid = (float(a.open) + float(a.close)) / 2
    return bull(a) and bear(b) and float(b.open) > float(a.high) and float(b.close) < mid and float(b.close) > float(a.open)


def is_bullish_harami(a, b, cfg=EngineConfig()):
    return bear(a) and large_body(a, cfg.min_large_body_multiple) and bull(b) and body_high(b) < body_high(a) and body_low(b) > body_low(a) and body(b) < body(a)


def is_bearish_harami(a, b, cfg=EngineConfig()):
    return bull(a) and large_body(a, cfg.min_large_body_multiple) and bear(b) and body_high(b) < body_high(a) and body_low(b) > body_low(a) and body(b) < body(a)


def is_tweezer_bottom(a, b, cfg=EngineConfig()):
    tol = min(float(a.low), float(b.low)) * cfg.tweezer_tolerance_pct / 100
    return bear(a) and bull(b) and abs(float(a.low) - float(b.low)) <= tol


def is_tweezer_top(a, b, cfg=EngineConfig()):
    tol = max(float(a.high), float(b.high)) * cfg.tweezer_tolerance_pct / 100
    return bull(a) and bear(b) and abs(float(a.high) - float(b.high)) <= tol


def is_bullish_kicker(a, b, cfg=EngineConfig()):
    return bear(a) and bull(b) and large_body(a, cfg.min_large_body_multiple) and large_body(b, cfg.min_large_body_multiple) and float(b.low) > float(a.high)


def is_bearish_kicker(a, b, cfg=EngineConfig()):
    return bull(a) and bear(b) and large_body(a, cfg.min_large_body_multiple) and large_body(b, cfg.min_large_body_multiple) and float(b.high) < float(a.low)


def is_morning_star(a, b, c, cfg=EngineConfig()):
    mid = (float(a.open) + float(a.close)) / 2
    return bear(a) and large_body(a, cfg.min_large_body_multiple) and body(b) <= cfg.star_max_body_ratio_to_first * body(a) and bull(c) and float(c.close) > mid


def is_evening_star(a, b, c, cfg=EngineConfig()):
    mid = (float(a.open) + float(a.close)) / 2
    return bull(a) and large_body(a, cfg.min_large_body_multiple) and body(b) <= cfg.star_max_body_ratio_to_first * body(a) and bear(c) and float(c.close) < mid


def closes_near_high(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(r > 0 and float(c.high) - float(c.close) <= cfg.close_near_extreme_ratio * r)


def closes_near_low(c, cfg=EngineConfig()):
    r = rng(c)
    return bool(r > 0 and float(c.close) - float(c.low) <= cfg.close_near_extreme_ratio * r)


def is_three_white_soldiers(a, b, c, cfg=EngineConfig()):
    return all(bull(x) and large_body(x, cfg.min_large_body_multiple) and closes_near_high(x, cfg) for x in (a, b, c)) and float(a.close) < float(b.close) < float(c.close)


def is_three_black_crows(a, b, c, cfg=EngineConfig()):
    return all(bear(x) and large_body(x, cfg.min_large_body_multiple) and closes_near_low(x, cfg) for x in (a, b, c)) and float(a.close) > float(b.close) > float(c.close)


def is_three_inside_up(a, b, c, cfg=EngineConfig()):
    return is_bullish_harami(a, b, cfg) and bull(c) and float(c.close) > float(a.high)


def is_three_inside_down(a, b, c, cfg=EngineConfig()):
    return is_bearish_harami(a, b, cfg) and bear(c) and float(c.close) < float(a.low)


def is_rising_three_methods(bars, cfg=EngineConfig()):
    a, *mids, e = bars
    return bull(a) and large_body(a, cfg.min_large_body_multiple) and all(bear(x) and float(x.high) < float(a.high) and float(x.low) > float(a.low) for x in mids) and bull(e) and large_body(e, cfg.min_large_body_multiple) and float(e.close) > float(a.high)


def is_falling_three_methods(bars, cfg=EngineConfig()):
    a, *mids, e = bars
    return bear(a) and large_body(a, cfg.min_large_body_multiple) and all(bull(x) and float(x.high) < float(a.high) and float(x.low) > float(a.low) for x in mids) and bear(e) and large_body(e, cfg.min_large_body_multiple) and float(e.close) < float(a.low)


def inside_squeeze_indices(df, i):
    if i < 1:
        return None
    for mother in range(i - 1, max(-1, i - 6), -1):
        m = df.iloc[mother]
        if all(float(df.iloc[j].high) < float(m.high) and float(df.iloc[j].low) > float(m.low) for j in range(mother + 1, i + 1)):
            return tuple(range(mother, i + 1))
    return None


def _setup(pattern, side, trigger, i, idxs, trigger_price, stop, reason):
    return Setup(pattern, side, trigger, i, tuple(idxs), trigger_price, float(stop), reason)


# ----------------------------- setup detection -----------------------------

def detect_setups(df, i, tick, cfg=EngineConfig(), intended_side: Optional[Side] = None):
    """Detect all qualifying patterns, optionally restricted to intended_side."""
    out = []
    c = df.iloc[i]

    if volume_ok(c):
        singles = [
            (is_bullish_marubozu(c, cfg), Pattern.BULLISH_MARUBOZU, Side.BUY, Trigger.CLOSED_CANDLE, None, float(c.low) - tick),
            (is_bearish_marubozu(c, cfg), Pattern.BEARISH_MARUBOZU, Side.SELL, Trigger.CLOSED_CANDLE, None, float(c.high) + tick),
            (is_hammer(c, cfg), Pattern.HAMMER, Side.BUY, Trigger.BREAKOUT, float(c.high) + tick, float(c.low) - tick),
            (is_inverted_hammer(c, cfg), Pattern.INVERTED_HAMMER, Side.BUY, Trigger.BREAKOUT, float(c.high) + tick, float(c.low) - tick),
            (is_shooting_star(c, cfg), Pattern.SHOOTING_STAR, Side.SELL, Trigger.BREAKOUT, float(c.low) - tick, float(c.high) + tick),
            (is_hanging_man(c, cfg), Pattern.HANGING_MAN, Side.SELL, Trigger.BREAKOUT, float(c.low) - tick, float(c.high) + tick),
        ]
        for ok, p, s, t, tr, sl in singles:
            if ok:
                out.append(_setup(p, s, t, i, (i,), tr, sl, p.value))
        if is_dragonfly_doji(c, cfg):
            out.append(_setup(Pattern.DRAGONFLY_DOJI, Side.BUY, Trigger.BREAKOUT, i, (i,), float(c.high) + tick, float(c.low) - tick, "dragonfly Doji"))
        elif is_gravestone_doji(c, cfg):
            out.append(_setup(Pattern.GRAVESTONE_DOJI, Side.SELL, Trigger.BREAKOUT, i, (i,), float(c.low) - tick, float(c.high) + tick, "gravestone Doji"))
        elif is_doji(c, cfg):
            out.append(_setup(Pattern.DOJI, Side.BUY, Trigger.BREAKOUT, i, (i,), float(c.high) + tick, float(c.low) - tick, "Doji bullish"))
            out.append(_setup(Pattern.DOJI, Side.SELL, Trigger.BREAKOUT, i, (i,), float(c.low) - tick, float(c.high) + tick, "Doji bearish"))

    if i >= 1:
        a, b = df.iloc[i - 1], c
        idx = (i - 1, i)
        if all_volume_ok(df, idx):
            pairs = [
                (is_bullish_engulfing(a, b), Pattern.BULLISH_ENGULFING, Side.BUY, Trigger.NEXT_OPEN, None, min(float(a.low), float(b.low)) - tick),
                (is_bearish_engulfing(a, b), Pattern.BEARISH_ENGULFING, Side.SELL, Trigger.NEXT_OPEN, None, max(float(a.high), float(b.high)) + tick),
                (is_piercing_line(a, b), Pattern.PIERCING_LINE, Side.BUY, Trigger.NEXT_OPEN, None, float(b.low) - tick),
                (is_dark_cloud_cover(a, b), Pattern.DARK_CLOUD_COVER, Side.SELL, Trigger.NEXT_OPEN, None, float(b.high) + tick),
                (is_bullish_harami(a, b, cfg), Pattern.BULLISH_HARAMI, Side.BUY, Trigger.BREAKOUT, float(a.high) + tick, float(a.low) - tick),
                (is_bearish_harami(a, b, cfg), Pattern.BEARISH_HARAMI, Side.SELL, Trigger.BREAKOUT, float(a.low) - tick, float(a.high) + tick),
                (is_tweezer_bottom(a, b, cfg), Pattern.TWEEZER_BOTTOM, Side.BUY, Trigger.CLOSED_CANDLE, None, min(float(a.low), float(b.low)) - tick),
                (is_tweezer_top(a, b, cfg), Pattern.TWEEZER_TOP, Side.SELL, Trigger.CLOSED_CANDLE, None, max(float(a.high), float(b.high)) + tick),
                (is_bullish_kicker(a, b, cfg), Pattern.BULLISH_KICKER, Side.BUY, Trigger.CLOSED_CANDLE, None, float(a.low) - tick),
                (is_bearish_kicker(a, b, cfg), Pattern.BEARISH_KICKER, Side.SELL, Trigger.CLOSED_CANDLE, None, float(a.high) + tick),
            ]
            for ok, p, s, t, tr, sl in pairs:
                if ok:
                    out.append(_setup(p, s, t, i, idx, tr, sl, p.value))

    if i >= 2:
        a, b, d = df.iloc[i - 2], df.iloc[i - 1], c
        idx = (i - 2, i - 1, i)
        if all_volume_ok(df, idx):
            triples = [
                (is_morning_star(a, b, d, cfg), Pattern.MORNING_STAR, Side.BUY, min(float(b.low), float(d.low)) - tick),
                (is_evening_star(a, b, d, cfg), Pattern.EVENING_STAR, Side.SELL, max(float(b.high), float(d.high)) + tick),
                (is_three_white_soldiers(a, b, d, cfg), Pattern.THREE_WHITE_SOLDIERS, Side.BUY, float(a.low) - tick),
                (is_three_black_crows(a, b, d, cfg), Pattern.THREE_BLACK_CROWS, Side.SELL, float(a.high) + tick),
                (is_three_inside_up(a, b, d, cfg), Pattern.THREE_INSIDE_UP, Side.BUY, min(float(a.low), float(b.low)) - tick),
                (is_three_inside_down(a, b, d, cfg), Pattern.THREE_INSIDE_DOWN, Side.SELL, max(float(a.high), float(b.high)) + tick),
            ]
            for ok, p, s, sl in triples:
                if ok:
                    out.append(_setup(p, s, Trigger.CLOSED_CANDLE, i, idx, None, sl, p.value))

    if i >= 4:
        idx = tuple(range(i - 4, i + 1))
        bars = [df.iloc[j] for j in idx]
        if all_volume_ok(df, idx):
            if is_rising_three_methods(bars, cfg):
                out.append(_setup(Pattern.RISING_THREE_METHODS, Side.BUY, Trigger.CLOSED_CANDLE, i, idx, None, min(float(x.low) for x in bars[1:4]) - tick, "rising three methods"))
            if is_falling_three_methods(bars, cfg):
                out.append(_setup(Pattern.FALLING_THREE_METHODS, Side.SELL, Trigger.CLOSED_CANDLE, i, idx, None, max(float(x.high) for x in bars[1:4]) + tick, "falling three methods"))

    sq = inside_squeeze_indices(df, i)
    if sq and all_volume_ok(df, sq):
        mother = df.iloc[sq[0]]
        latest = df.iloc[sq[-1]]
        out.append(_setup(Pattern.INSIDE_BAR_SQUEEZE, Side.BUY, Trigger.BREAKOUT, i, sq, float(mother.high) + tick, float(latest.low) - tick, "inside squeeze bullish"))
        out.append(_setup(Pattern.INSIDE_BAR_SQUEEZE, Side.SELL, Trigger.BREAKOUT, i, sq, float(mother.low) - tick, float(latest.high) + tick, "inside squeeze bearish"))

    if intended_side is not None:
        out = [s for s in out if s.side == intended_side]
    return out


# ----------------------------- risk / plan construction -----------------------------

def position_size(equity, entry, stop, risk_pct):
    per_share = abs(entry - stop)
    if equity <= 0 or per_share <= 0 or risk_pct <= 0:
        return 0
    return max(math.floor((equity * risk_pct / 100.0) / per_share), 0)


def build_trade_plan(setup, entry_index, entry, equity, cfg=EngineConfig()):
    if setup.side == Side.BUY and setup.stop_price >= entry:
        return None
    if setup.side == Side.SELL and setup.stop_price <= entry:
        return None
    risk = abs(entry - setup.stop_price)
    if risk <= 0:
        return None
    target = entry + cfg.min_rr * risk if setup.side == Side.BUY else entry - cfg.min_rr * risk
    qty = position_size(equity, entry, setup.stop_price, cfg.risk_pct)
    if qty <= 0:
        return None
    planned = risk * qty
    max_allowed = equity * cfg.risk_pct / 100.0
    if planned > max_allowed + 1e-9:
        return None
    return TradePlan(setup.pattern, setup.side, setup.trigger, setup.setup_index, entry_index,
                     float(entry), setup.stop_price, float(target), qty, risk, planned,
                     cfg.min_rr, setup.reason)


def plan_from_closed_setup(df, setup, i, equity, cfg=EngineConfig()):
    c = df.iloc[i]
    if not context_ok(c, setup.side):
        return None
    return build_trade_plan(setup, i, float(c.close), equity, cfg)


def confirm_pending(df, setup, i, equity, cfg=EngineConfig()):
    if i <= setup.setup_index or i - setup.setup_index > cfg.max_wait_bars:
        return None
    c = df.iloc[i]
    if not volume_ok(c) or not context_ok(c, setup.side):
        return None
    if setup.trigger == Trigger.NEXT_OPEN:
        if i != setup.setup_index + 1:
            return None
        return build_trade_plan(setup, i, float(c.open), equity, cfg)
    if setup.trigger != Trigger.BREAKOUT or setup.trigger_price is None:
        return None
    # Closed-candle confirmation only: intrabar touch does not count.
    if setup.side == Side.BUY and float(c.close) <= setup.trigger_price:
        return None
    if setup.side == Side.SELL and float(c.close) >= setup.trigger_price:
        return None
    return build_trade_plan(setup, i, float(c.close), equity, cfg)


# ----------------------------- entry timing router -----------------------------

def evaluate_trade_entry(symbol, completed_df, intended_direction, account_equity,
                         tick_size, engine=None) -> EntryGateResult:
    """Final candlestick timing gate downstream of ADX/RSI/PA/MA.

    Outcomes:
      NO_PATTERN -> block/no order
      WAITING    -> persist pending setup/no order
      CONFIRMED  -> return 2R geometric plan sized at cfg.risk_pct (0.20% default)

    This function does not decide direction and never submits orders.
    """
    engine = engine or CandlestickEngine(EngineConfig(risk_pct=0.20))
    cfg = engine.config
    direction = str(intended_direction).upper().strip()
    if direction not in {"BUY", "SELL"}:
        return EntryGateResult(GateState.NO_PATTERN, direction, "INVALID_OR_BLOCKED_DIRECTION")
    side = Side(direction)
    if account_equity <= 0 or tick_size <= 0 or completed_df is None or completed_df.empty:
        return EntryGateResult(GateState.NO_PATTERN, direction, "INVALID_INPUT")

    df = add_indicators(completed_df, cfg)
    warmup = max(cfg.volume_lookback, cfg.body_lookback) + 5
    if len(df) < warmup:
        return EntryGateResult(GateState.NO_PATTERN, direction, "INSUFFICIENT_CLOSED_BARS")
    i = len(df) - 1

    # 1) Confirm only pending setups matching the already-decided side.
    existing = engine.pending.get(symbol, [])
    keep = []
    for setup in existing:
        if setup.side != side:
            continue
        plan = confirm_pending(df, setup, i, account_equity, cfg)
        if plan is not None:
            engine.pending[symbol] = [s for s in keep if s.side == side]
            logger.info("CANDLE GATE CONFIRMED | %s | %s | %s | entry=%.4f SL=%.4f TP=%.4f qty=%d risk=%.2f RR=%.2f",
                        symbol, plan.pattern.value, plan.side.value, plan.entry_price,
                        plan.stop_price, plan.target_price, plan.quantity, plan.planned_risk, plan.rr)
            return EntryGateResult(GateState.CONFIRMED, direction, "CONFIRMED_PATTERN", plan.pattern, None, plan)
        if i - setup.setup_index <= cfg.max_wait_bars:
            keep.append(setup)
    engine.pending[symbol] = keep

    # 2) Detect ONLY patterns compatible with upstream intended direction.
    setups = detect_setups(df, i, tick_size, cfg, intended_side=side)
    if not setups:
        if keep:
            return EntryGateResult(GateState.WAITING, direction, "WAITING_FOR_EXISTING_PATTERN", keep[0].pattern, keep[0], None)
        return EntryGateResult(GateState.NO_PATTERN, direction, "NO_DIRECTION_MATCHING_PATTERN")

    # 3) Closed-candle patterns may confirm immediately if context is valid.
    for setup in setups:
        if setup.trigger == Trigger.CLOSED_CANDLE:
            plan = plan_from_closed_setup(df, setup, i, account_equity, cfg)
            if plan is not None:
                logger.info("CANDLE GATE CONFIRMED | %s | %s | %s | entry=%.4f SL=%.4f TP=%.4f qty=%d risk=%.2f RR=%.2f",
                            symbol, plan.pattern.value, plan.side.value, plan.entry_price,
                            plan.stop_price, plan.target_price, plan.quantity, plan.planned_risk, plan.rr)
                return EntryGateResult(GateState.CONFIRMED, direction, "CONFIRMED_PATTERN", plan.pattern, None, plan)

    # 4) Breakout / next-open patterns become pending; no order now.
    waiting = [s for s in setups if s.trigger in {Trigger.BREAKOUT, Trigger.NEXT_OPEN}]
    if waiting:
        # Deduplicate by pattern/setup index/side.
        known = {(s.pattern, s.setup_index, s.side) for s in engine.pending.get(symbol, [])}
        for setup in waiting:
            key = (setup.pattern, setup.setup_index, setup.side)
            if key not in known:
                engine.pending.setdefault(symbol, []).append(setup)
                known.add(key)
        chosen = waiting[0]
        logger.info("CANDLE GATE WAITING | %s | %s | %s | trigger=%s",
                    symbol, chosen.pattern.value, chosen.side.value, chosen.trigger_price)
        return EntryGateResult(GateState.WAITING, direction, "PATTERN_FOUND_WAITING_CONFIRMATION", chosen.pattern, chosen, None)

    return EntryGateResult(GateState.NO_PATTERN, direction, "PATTERN_FAILED_CONTEXT_OR_RISK")
