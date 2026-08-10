"""
Streaming candle builder (Phase 2 of the WS candle engine).

Builds configured entry candles from ws_ticker.TickBuffer ticks, and 15-min
candles by combining a complete set of finalized entry candles -- never built
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
    """Build finalized candles for one symbol from ticks fed in order."""

    def __init__(self, symbol: str, interval_minutes: int = 5):
        self.symbol = symbol
        self.interval_minutes = interval_minutes
        self._current: Optional[_InProgressCandle] = None
        self.finalized: list[dict] = []  # ordered, oldest first

    def add_tick(self, tick: dict) -> Optional[dict]:
        """
        Feeds one tick (as produced by ws_ticker.TickBuffer). Returns the
        finalized candle dict if this tick closed out the previous
        interval, else None.
        """
        ts = tick["exchange_timestamp"]
        price = tick["last_price"]
        cum_volume = tick.get("volume_traded")
        if price is None:
            return None

        interval_start = _interval_start(ts, self.interval_minutes)
        closed = None

        if self._current is None:
            self._current = _InProgressCandle(self.symbol, interval_start, price, price, price, price, 0.0)
            self._current.update(price, cum_volume)
            return None

        if interval_start > self._current.start:
            # Preserve the cumulative-volume baseline across the boundary.
            # Without this hand-off, the difference between the final tick
            # of the old interval and the first tick of the new interval is
            # silently discarded from every candle after startup.
            previous_cum_volume = self._current._last_cum_volume
            closed = self._current.finalize()
            self.finalized.append(closed)
            self._current = _InProgressCandle(
                self.symbol,
                interval_start,
                price,
                price,
                price,
                price,
                0.0,
                _last_cum_volume=previous_cum_volume,
            )

        self._current.update(price, cum_volume)
        return closed

    def finalized_df(self) -> pd.DataFrame:
        """Never includes the still-forming candle -- only fully closed ones."""
        if not self.finalized:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        return pd.DataFrame(self.finalized)


def combine_entry_into_15m(
    entry_candles: list[dict],
    entry_interval_minutes: int,
) -> list[dict]:
    """
    Combine finalized entry candles into exact wall-clock 15-minute bars.

    The entry interval must divide 15 exactly. For the production 3-minute
    timeframe this requires five distinct legs at offsets 0, 3, 6, 9 and
    12 minutes. Duplicate, missing, misaligned, or extra legs fail closed.
    """
    interval = int(entry_interval_minutes)
    if interval <= 0 or 15 % interval != 0:
        raise ValueError("entry interval must divide 15 minutes exactly")

    if not entry_candles:
        return []

    groups: dict[datetime, list[dict]] = {}
    for c in entry_candles:
        bucket_start = _interval_start(c["date"], 15)
        groups.setdefault(bucket_start, []).append(c)

    out = []
    for bucket_start in sorted(groups):
        legs = sorted(groups[bucket_start], key=lambda c: c["date"])
        expected_dates = [
            bucket_start + timedelta(minutes=offset)
            for offset in range(0, 15, interval)
        ]
        if (
            len(legs) != len(expected_dates)
            or [c["date"] for c in legs] != expected_dates
        ):
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


def combine_5m_into_15m(candles_5m: list[dict]) -> list[dict]:
    """Backward-compatible wrapper retained for historical validators."""

    return combine_entry_into_15m(candles_5m, 5)


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

    def compare_entry_candle(
        self,
        symbol: str,
        exchange: str,
        ws_candle: dict,
        instrument_token: int,
        interval: str,
    ):
        from data_feed import fetch_candles

        rest_df = fetch_candles(
            self.kite,
            instrument_token,
            interval,
            lookback_days=1,
        )
        if rest_df.empty:
            record = {"symbol": symbol, "timeframe": interval, "date": ws_candle["date"].isoformat(),
                      "status": "no_rest_data"}
            self._write(record)
            return record

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
            record = {"symbol": symbol, "timeframe": interval, "date": ws_candle["date"].isoformat(),
                      "status": "no_matching_rest_candle", "note": "tz_normalization_failed"}
            self._write(record)
            return record

        rest_row = rest_df[rest_df["date"] == ws_date]
        if rest_row.empty:
            record = {"symbol": symbol, "timeframe": interval, "date": ws_candle["date"].isoformat(),
                      "status": "no_matching_rest_candle"}
            self._write(record)
            return record

        rest = rest_row.iloc[0]
        record = {"symbol": symbol, "timeframe": interval, "date": ws_candle["date"].isoformat(),
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
            logger.warning(
                f"candle_engine shadow mismatch: {symbol} {interval} "
                f"{ws_candle['date']} -- {record}"
            )
        self._write(record)
        return record

    def compare_5m_candle(
        self,
        symbol: str,
        exchange: str,
        ws_candle: dict,
        instrument_token: int,
    ):
        """Backward-compatible wrapper for historical tests/tools."""

        return self.compare_entry_candle(
            symbol,
            exchange,
            ws_candle,
            instrument_token,
            "5minute",
        )

    def _write(self, record: dict):
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self._log_path(), "a") as f:
                f.write(line + "\n")
