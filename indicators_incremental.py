"""
Incremental indicator updates (Phase 3 of the WS candle engine).

Mirrors indicators.py's EMA/VWAP/ATR/ADX math EXACTLY, including its
specific NaN/seeding behavior -- these are not a "textbook Wilder's
method" implementation, they are a step-by-step replication of what
pandas' `.ewm(..., adjust=False)` actually computes, since that's what
indicators.py uses and what strategy.evaluate() has been backtested
and traded against. Diverging from that math, even in a "more
correct" direction, would silently change signal behavior.

On bot restart / cold start: seed every incremental state from the
last N REST-fetched candles using indicators.add_indicators() (the
existing batch path) BEFORE switching to incremental updates -- see
IncrementalIndicatorState.seed_from_history(). Never start incremental
state from zero.

In shadow mode, call compare_against_batch() periodically (e.g. every
30 min) to recompute indicators from scratch via the existing batch
functions and log the delta against the incrementally maintained
values, same pattern as candle_engine.ShadowComparator.
"""

import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("indicators_incremental")

SHADOW_LOG_DIR = "ws_shadow_logs"


def _ewm_step(prev: Optional[float], x: float, alpha: float) -> float:
    """
    One step of pandas' `.ewm(alpha=alpha, adjust=False).mean()` recursion:
    y_t = x_t if this is the first (non-NaN) value ever seen, else
    y_t = alpha * x_t + (1 - alpha) * y_{t-1}.
    `prev=None` means "not yet seeded" -- matches pandas seeding from
    the first non-NaN input rather than a simple-moving-average seed.
    """
    if prev is None:
        return x
    return alpha * x + (1 - alpha) * prev


@dataclass
class _ADXState:
    """
    Matches indicators.adx() exactly, including pandas ewm's
    min_periods=period gating -- NOT just the recursion formula.
    indicators.adx() computes smoothed_tr/plus_dm/minus_dm via
    `.ewm(alpha=1/period, adjust=False, min_periods=period).mean()`,
    which internally recurses from row 0 but reports NaN until `period`
    rows have been processed. dx is then NaN until smoothed_tr is
    valid, and the outer `dx.ewm(..., min_periods=period).mean()`
    itself needs `period` non-NaN dx values before it reports a value.
    Net effect, verified against the batch function: ADX(period) first
    becomes non-NaN at bar index (2*period - 2), 0-indexed.
    """
    period: int = 14
    bar_count: int = 0
    valid_dx_count: int = 0
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    prev_close: Optional[float] = None
    smoothed_tr: Optional[float] = None
    smoothed_plus_dm: Optional[float] = None
    smoothed_minus_dm: Optional[float] = None
    adx: Optional[float] = None  # the second-stage ewm seed/state

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        alpha = 1.0 / self.period
        self.bar_count += 1

        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))

        if self.prev_high is None or self.prev_low is None:
            up_move = float("nan")
            down_move = float("nan")
        else:
            up_move = high - self.prev_high
            down_move = self.prev_low - low

        plus_dm = up_move if (not math.isnan(up_move) and up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (not math.isnan(down_move) and down_move > up_move and down_move > 0) else 0.0

        # Recursion always advances internally (matches pandas' actual
        # behavior under the NaN mask), even while gated below.
        self.smoothed_tr = _ewm_step(self.smoothed_tr, tr, alpha)
        self.smoothed_plus_dm = _ewm_step(self.smoothed_plus_dm, plus_dm, alpha)
        self.smoothed_minus_dm = _ewm_step(self.smoothed_minus_dm, minus_dm, alpha)

        self.prev_high, self.prev_low, self.prev_close = high, low, close

        smoothed_tr_valid = self.bar_count >= self.period
        if not smoothed_tr_valid or not self.smoothed_tr:
            dx = float("nan")
        else:
            plus_di = 100 * (self.smoothed_plus_dm / self.smoothed_tr)
            minus_di = 100 * (self.smoothed_minus_dm / self.smoothed_tr)
            denom = plus_di + minus_di
            dx = float("nan") if denom == 0 else 100 * abs(plus_di - minus_di) / denom

        if not math.isnan(dx):
            self.valid_dx_count += 1
            self.adx = _ewm_step(self.adx, dx, alpha)
            # a NaN dx leaves self.adx unchanged, matching pandas ewm's
            # "skip leading NaNs until first real value" seeding behavior

        if self.valid_dx_count < self.period:
            return None  # matches the outer ewm's own min_periods=period gate
        return self.adx


@dataclass
class SymbolIndicatorState:
    """
    All incremental indicator state for one symbol, one timeframe.
    ema_periods maps e.g. {20: ema20_value, 50: ema50_value} so the
    same state object can serve both TREND_EMA_FAST/SLOW (15-min) or
    ENTRY_EMA (5-min) depending on which timeframe it's attached to.
    """
    symbol: str
    ema_periods: dict = field(default_factory=dict)       # {period: last_ema_value}
    vwap_day: Optional[object] = None                     # date the cumulative sums belong to
    vwap_cum_tp_vol: float = 0.0
    vwap_cum_vol: float = 0.0
    atr_value: Optional[float] = None
    atr_period: int = 14
    prev_close_for_atr: Optional[float] = None
    adx_state: _ADXState = field(default_factory=_ADXState)
    volume_window: list = field(default_factory=list)     # rolling window for average_volume
    volume_window_size: int = 20

    def update_ema(self, period: int, close: float, span: Optional[int] = None) -> float:
        alpha = 2.0 / ((span or period) + 1)  # matches df.ewm(span=period, adjust=False)
        prev = self.ema_periods.get(period)
        new = _ewm_step(prev, close, alpha)
        self.ema_periods[period] = new
        return new

    def update_vwap(self, day, high: float, low: float, close: float, volume: float) -> Optional[float]:
        if self.vwap_day != day:
            self.vwap_day = day
            self.vwap_cum_tp_vol = 0.0
            self.vwap_cum_vol = 0.0
        typical_price = (high + low + close) / 3
        self.vwap_cum_tp_vol += typical_price * volume
        self.vwap_cum_vol += volume
        if self.vwap_cum_vol == 0:
            return None
        return self.vwap_cum_tp_vol / self.vwap_cum_vol

    def update_atr(self, high: float, low: float, close: float) -> Optional[float]:
        """
        NOTE: indicators.atr() is a SIMPLE rolling mean of true range,
        not Wilder-smoothed -- unlike adx(). A true O(1) incremental
        simple-rolling-mean needs the full window (rolling().mean() is
        not a recursive EMA), so this keeps a bounded window rather
        than a single scalar. Matches df["tr"].rolling(period).mean()
        exactly, including the NaN period before the window fills.
        """
        if self.prev_close_for_atr is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close_for_atr), abs(low - self.prev_close_for_atr))
        self.prev_close_for_atr = close

        window = getattr(self, "_atr_window", None)
        if window is None:
            window = []
            self._atr_window = window
        window.append(tr)
        if len(window) > self.atr_period:
            window.pop(0)
        if len(window) < self.atr_period:
            self.atr_value = None
        else:
            self.atr_value = sum(window) / self.atr_period
        return self.atr_value

    def update_adx(self, high: float, low: float, close: float) -> Optional[float]:
        return self.adx_state.update(high, low, close)

    def update_volume_avg(self, volume: float) -> Optional[float]:
        self.volume_window.append(volume)
        if len(self.volume_window) > self.volume_window_size:
            self.volume_window.pop(0)
        if len(self.volume_window) < self.volume_window_size:
            return None
        return sum(self.volume_window) / self.volume_window_size

    def seed_from_history(self, df: pd.DataFrame, cfg, timeframe: str):
        """
        Seeds this state from a REST-fetched history DataFrame using the
        EXISTING batch indicators module, so incremental updates continue
        seamlessly from wherever the batch computation left off -- never
        starts cold from an empty/zero state.
        `timeframe`: "15minute" or "5minute", controls which periods get seeded.
        """
        from indicators import ema, vwap, atr, adx, average_volume

        if df.empty:
            return

        if timeframe == "15minute":
            fast_col = ema(df, cfg.TREND_EMA_FAST)
            slow_col = ema(df, cfg.TREND_EMA_SLOW)
            self.ema_periods[cfg.TREND_EMA_FAST] = float(fast_col.iloc[-1])
            self.ema_periods[cfg.TREND_EMA_SLOW] = float(slow_col.iloc[-1])

            vwap_col = vwap(df)
            last_day = df["date"].iloc[-1].date()
            self.vwap_day = last_day
            same_day = df[df["date"].dt.date == last_day]
            typical = (same_day["high"] + same_day["low"] + same_day["close"]) / 3
            self.vwap_cum_tp_vol = float((typical * same_day["volume"]).sum())
            self.vwap_cum_vol = float(same_day["volume"].sum())

            period = getattr(cfg, "ADX_PERIOD", 14)
            adx_col = adx(df, period)
            last_adx = adx_col.iloc[-1]
            self.adx_state.adx = None if pd.isna(last_adx) else float(last_adx)
            self.adx_state.prev_high = float(df["high"].iloc[-1])
            self.adx_state.prev_low = float(df["low"].iloc[-1])
            self.adx_state.prev_close = float(df["close"].iloc[-1])
            # Restore the gating counters so continuation doesn't re-trigger
            # the "warming up" NaN period: if history was already long
            # enough to produce a real ADX value, both counters are past
            # threshold; otherwise mirror how many bars/valid-dx history
            # actually provided so gating continues correctly.
            self.adx_state.bar_count = len(df)
            self.adx_state.valid_dx_count = max(0, len(df) - (period - 1)) if len(df) >= period else 0
            # smoothed_tr/plus_dm/minus_dm aren't individually recoverable
            # from the final adx value alone -- recompute them the same
            # way indicators.adx() does, then take their last values.
            self.adx_state.smoothed_tr = None
            self.adx_state.smoothed_plus_dm = None
            self.adx_state.smoothed_minus_dm = None
            self._seed_adx_smoothed_components(df, getattr(cfg, "ADX_PERIOD", 14))

        elif timeframe == "5minute":
            entry_col = ema(df, cfg.ENTRY_EMA)
            self.ema_periods[cfg.ENTRY_EMA] = float(entry_col.iloc[-1])

            avg_vol_col = average_volume(df, cfg.VOLUME_LOOKBACK)
            self.volume_window_size = cfg.VOLUME_LOOKBACK
            tail = df["volume"].tail(cfg.VOLUME_LOOKBACK)
            self.volume_window = [float(v) for v in tail.tolist()] if len(tail) == cfg.VOLUME_LOOKBACK else []

            atr_col = atr(df, self.atr_period)
            self.prev_close_for_atr = float(df["close"].iloc[-1])
            tail_tr = self._recompute_tr_window(df, self.atr_period)
            self._atr_window = tail_tr
            self.atr_value = None if pd.isna(atr_col.iloc[-1]) else float(atr_col.iloc[-1])

    def _seed_adx_smoothed_components(self, df: pd.DataFrame, period: int):
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        prev_high, prev_low = high.shift(1), low.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        up_move, down_move = high - prev_high, prev_low - low
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]
        alpha = 1.0 / period
        self.adx_state.smoothed_tr = float(tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean().iloc[-1])
        self.adx_state.smoothed_plus_dm = float(plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean().iloc[-1])
        self.adx_state.smoothed_minus_dm = float(minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean().iloc[-1])

    def _recompute_tr_window(self, df: pd.DataFrame, period: int) -> list:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        tail = tr.tail(period).tolist()
        return [float(v) for v in tail]


class IncrementalShadowComparator:
    """
    Periodically recomputes indicators from scratch via the existing
    batch functions (indicators.add_indicators equivalent) and logs the
    delta against the incrementally maintained SymbolIndicatorState, so
    drift can be caught before WS_CANDLE_MODE is ever set to "live".
    """

    TOLERANCE = 0.05  # absolute, generous enough to absorb float accumulation error

    def __init__(self, log_dir: str = SHADOW_LOG_DIR):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        os.makedirs(self.log_dir, exist_ok=True)

    def compare(self, symbol: str, timeframe: str, incremental_values: dict, batch_values: dict):
        record = {"symbol": symbol, "timeframe": timeframe, "type": "indicator_comparison"}
        within_tolerance = True
        for key, inc_val in incremental_values.items():
            batch_val = batch_values.get(key)
            if batch_val is None or inc_val is None or (isinstance(batch_val, float) and math.isnan(batch_val)):
                record[key] = {"incremental": inc_val, "batch": batch_val, "delta": None}
                continue
            delta = abs(inc_val - batch_val)
            record[key] = {"incremental": inc_val, "batch": batch_val, "delta": delta}
            if delta > self.TOLERANCE:
                within_tolerance = False
        record["within_tolerance"] = within_tolerance
        if not within_tolerance:
            logger.warning(f"indicators_incremental shadow mismatch: {symbol} {timeframe} -- {record}")
        self._write(record)

    def _write(self, record: dict):
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        path = os.path.join(self.log_dir, f"ws_shadow_indicators_{datetime.now():%Y-%m-%d}.jsonl")
        with self._lock:
            with open(path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
