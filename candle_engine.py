"""
Streaming candle builder (Phase 2 of the WS candle engine).

Builds 5-min candles from ws_ticker.TickBuffer ticks, and 15-min
candles by combining three finalized 5-min candles -- never built
independently from ticks, so the two timeframes can't drift apart the
way they could if each aggregated raw ticks on its own clock.

Shadow mode (cfg.WS_CANDLE_MODE == "shadow", the default once
ENABLE_WS_CANDLES is True): after finalizing a candle, immediately
fetch the equivalent REST candle via the existing data_feed.fetch_candles()
and log both side by side. Nothing here is consumed by strategy.evaluate()
or executor.place_entry_order() in shadow mode -- those keep using the
existing REST-polling path in main.py completely unchanged.

Live mode (cfg.WS_CANDLE_MODE == "live"): the caller (main.py, once
wired) uses get_finalized_candles() from here instead of
data_feed.fetch_candles() for the live signal-evaluation path. This
module does not flip that switch itself -- that wiring is a separate,
explicit change to main.py's scan loop, made only after shadow-mode
logs have been reviewed.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("candle_engine")

SHADOW_LOG_DIR = "ws_shadow_logs"


@dataclass
class _InProgressCandle:
    symbol: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float  # delta volume accumulated this candle, not cumulative
    _last_cum_volume: Optional[float] = field(default=None, repr=False)

    def update(self, price: float, cum_volume: Optional[float]):
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if cum_volume is not None:
            if self._last_cum_volume is not None and cum_volume >= self._last_cum_volume:
                self.volume += cum_volume - self._last_cum_volume
            self._last_cum_volume = cum_volume

    def finalize(self) -> dict:
        return {
            "date": self.start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def _interval_start(ts: datetime, minutes: int) -> datetime:
    """
    Floors `ts` to the start of its `minutes`-wide interval. A tick at
    exactly HH:MM:00.000 belongs to the interval STARTING at that
    timestamp (not the one ending there) -- e.g. with minutes=5, a tick
    at 09:30:00.000 starts the 09:30-09:35 candle, matching Kite's own
    historical-candle labeling convention (candles are labeled by their
    start time).
    """
    minutes_since_midnight = ts.hour * 60 + ts.minute
    floored = (minutes_since_midnight // minutes) * minutes
    return ts.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=floored)


class SymbolCandleBuilder:
    """Builds finalized 5-min candles for a single symbol from ticks fed in order.

    A WebSocket process can connect in the middle of a candle. That first
    interval is not a complete OHLCV candle because ticks from the beginning
    of the interval were never observed. Such a startup-partial interval is
    deliberately discarded instead of being compared with REST history,
    fed into incremental indicators, or exposed for live augmentation.

    Once an interval boundary is observed, subsequent candles are complete.
    The previous tick's cumulative day-volume is carried across the boundary
    as the new candle's volume baseline so the first inter-tick volume delta
    is not silently lost.
    """

    def __init__(self, symbol: str, interval_minutes: int = 5):
        self.symbol = symbol
        self.interval_minutes = interval_minutes
        self._current: Optional[_InProgressCandle] = None
        self._current_complete = False
        self.finalized: list[dict] = []  # ordered, oldest first

    def add_tick(self, tick: dict) -> Optional[dict]:
        """
        Feeds one tick (as produced by ws_ticker.TickBuffer). Returns the
        finalized candle dict if this tick closed out a complete previous
        interval, else None.

        The very first interval after process/WebSocket startup is returned
        only when its first observed tick is exactly on the interval boundary.
        If startup occurs mid-interval, that partial candle is discarded on
        the first rollover and normal full-candle emission begins immediately
        with the next interval.
        """
        ts = tick["exchange_timestamp"]
        price = tick["last_price"]
        cum_volume = tick.get("volume_traded")
        if price is None:
            return None

        interval_start = _interval_start(ts, self.interval_minutes)
        closed = None

        if self._current is None:
            self._current = _InProgressCandle(
                self.symbol, interval_start, price, price, price, price, 0.0
            )
            # Only an exact boundary-start tick proves we observed the whole
            # interval. A mid-candle process start cannot reconstruct prior
            # OHLCV safely from cumulative volume alone.
            self._current_complete = ts == interval_start
            self._current.update(price, cum_volume)
            return None

        if interval_start > self._current.start:
            previous_last_cum_volume = self._current._last_cum_volume

            if self._current_complete:
                closed = self._current.finalize()
                self.finalized.append(closed)
            else:
                logger.info(
                    "%s: discarded startup-partial %s-minute WS candle at %s",
                    self.symbol,
                    self.interval_minutes,
                    self._current.start,
                )

            self._current = _InProgressCandle(
                self.symbol, interval_start, price, price, price, price, 0.0
            )
            # From this boundary onward the builder has continuously observed
            # the feed. Carry the prior cumulative-volume baseline so the
            # volume between the last old-interval tick and first new-interval
            # tick is counted rather than dropped.
            self._current._last_cum_volume = previous_last_cum_volume
            self._current_complete = True

        self._current.update(price, cum_volume)
        return closed

    def finalized_df(self) -> pd.DataFrame:
        """Never includes the still-forming candle -- only fully closed ones."""
        if not self.finalized:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        return pd.DataFrame(self.finalized)


def combine_5m_into_15m(candles_5m: list[dict]) -> list[dict]:
    """
    Combines finalized 5-min candles into 15-min candles, three at a
    time, aligned to the wall clock (e.g. 09:15-09:30, not a rolling
    window). Only emits a 15-min candle once all three of its 5-min
    legs are present -- an incomplete trailing group is dropped, same
    "never expose a still-forming candle" rule as the 5-min builder.
    """
    if not candles_5m:
        return []

    groups: dict[datetime, list[dict]] = {}
    for c in candles_5m:
        bucket_start = _interval_start(c["date"], 15)
        groups.setdefault(bucket_start, []).append(c)

    out = []
    for bucket_start in sorted(groups):
        legs = sorted(groups[bucket_start], key=lambda c: c["date"])
        if len(legs) != 3:
            continue  # incomplete group -- don't emit a partial 15-min candle
        out.append({
            "date": bucket_start,
            "open": legs[0]["open"],
            "high": max(c["high"] for c in legs),
            "low": min(c["low"] for c in legs),
            "close": legs[-1]["close"],
            "volume": sum(c["volume"] for c in legs),
        })
    return out


class ShadowComparator:
    """
    Compares WS-built candles against the existing REST path
    (data_feed.fetch_candles) and writes one JSONL record per
    comparison to ws_shadow_logs/ so sessions can be reviewed offline
    before ever setting cfg.WS_CANDLE_MODE = "live".

    Tolerance is intentionally loose on volume (Kite's tick feed and
    historical-candle volume accounting can legitimately differ by a
    small amount) and tight on OHLC price fields.
    """

    PRICE_TOLERANCE = 0.01     # absolute rupees
    VOLUME_TOLERANCE_PCT = 2.0  # percent

    def __init__(self, kite, cfg, log_dir: str = SHADOW_LOG_DIR):
        self.kite = kite
        self.cfg = cfg
        self.log_dir = log_dir
        self._lock = threading.Lock()
        os.makedirs(self.log_dir, exist_ok=True)

    def _log_path(self, when: Optional[datetime] = None) -> str:
        when = when or datetime.now()
        return os.path.join(self.log_dir, f"ws_shadow_{when:%Y-%m-%d}.jsonl")

    def compare_5m_candle(self, symbol: str, exchange: str, ws_candle: dict, instrument_token: int):
        from data_feed import fetch_candles

        rest_df = fetch_candles(self.kite, instrument_token, "5minute", lookback_days=1)
        if rest_df.empty:
            self._write({"symbol": symbol, "timeframe": "5minute", "date": ws_candle["date"].isoformat(),
                         "status": "no_rest_data"})
            return

        # Normalize ws_candle["date"] to match rest_df["date"]'s timezone-awareness
        # before comparing. Without this, a naive ws_candle date (as
        # ws_ticker.py now correctly passes through Kite's raw tick
        # timestamps unmodified) compared against a tz-aware REST date
        # silently returns NO match for every single candle -- no
        # exception, no warning, just an empty result -- which is exactly
        # the bug found during today's end-of-day review (100% of
        # comparisons logged as "no_matching_rest_candle"). Same fix
        # pattern already used in ws_integration.get_augmented_candles.
        ws_date = pd.Timestamp(ws_candle["date"])
        rest_tz = rest_df["date"].dt.tz
        try:
            if rest_tz is not None and ws_date.tzinfo is None:
                ws_date = ws_date.tz_localize(rest_tz)
            elif rest_tz is None and ws_date.tzinfo is not None:
                ws_date = ws_date.tz_localize(None)
            elif rest_tz is not None and ws_date.tzinfo is not None:
                ws_date = ws_date.tz_convert(rest_tz)
        except Exception:
            logger.exception(f"candle_engine: timezone normalization failed for {symbol} -- "
                              f"logging as no match rather than risking a wrong comparison")
            self._write({"symbol": symbol, "timeframe": "5minute", "date": ws_candle["date"].isoformat(),
                         "status": "no_matching_rest_candle", "note": "tz_normalization_failed"})
            return

        rest_row = rest_df[rest_df["date"] == ws_date]
        if rest_row.empty:
            self._write({"symbol": symbol, "timeframe": "5minute", "date": ws_candle["date"].isoformat(),
                         "status": "no_matching_rest_candle"})
            return

        rest = rest_row.iloc[0]
        record = {"symbol": symbol, "timeframe": "5minute", "date": ws_candle["date"].isoformat(),
                  "status": "compared"}
        within_tolerance = True
        for field_name in ("open", "high", "low", "close"):
            delta = abs(ws_candle[field_name] - rest[field_name])
            record[f"{field_name}_ws"] = ws_candle[field_name]
            record[f"{field_name}_rest"] = float(rest[field_name])
            record[f"{field_name}_delta"] = delta
            if delta > self.PRICE_TOLERANCE:
                within_tolerance = False

        vol_delta_pct = 0.0
        if rest["volume"]:
            vol_delta_pct = abs(ws_candle["volume"] - rest["volume"]) / rest["volume"] * 100
        record["volume_ws"] = ws_candle["volume"]
        record["volume_rest"] = float(rest["volume"])
        record["volume_delta_pct"] = vol_delta_pct
        if vol_delta_pct > self.VOLUME_TOLERANCE_PCT:
            within_tolerance = False

        record["within_tolerance"] = within_tolerance
        if not within_tolerance:
            logger.warning(f"candle_engine shadow mismatch: {symbol} 5min {ws_candle['date']} -- {record}")
        self._write(record)

    def _write(self, record: dict):
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self._log_path(), "a") as f:
                f.write(line + "\n")
