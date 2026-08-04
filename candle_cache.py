"""Incremental historical-candle cache for the live scanner."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from data_feed import fetch_candles
from scheduler import candle_interval_minutes


class IncrementalCandleCache:
    """Reuse indicator history and request only the newest candles.

    A cached 15-minute series is reused until another 15-minute candle
    can have completed.  Five-minute data is refreshed on each new scan,
    using a short overlapping window and a date de-duplication merge.
    """

    def __init__(
        self,
        *,
        fetcher=fetch_candles,
        completion_buffer_seconds: int = 10,
    ):
        self._fetcher = fetcher
        self._completion_buffer_seconds = int(
            completion_buffer_seconds
        )
        self._frames = {}
        self.api_fetches = 0
        self.cache_hits = 0

    @staticmethod
    def _key(instrument_token, interval):
        return int(instrument_token), str(interval)

    @staticmethod
    def _normalise_now(now, frame):
        current = pd.Timestamp(now or datetime.now())

        if frame is None or frame.empty:
            return current

        latest = pd.Timestamp(frame["date"].iloc[-1])

        if latest.tzinfo is not None and current.tzinfo is None:
            return current.tz_localize(latest.tzinfo)

        if latest.tzinfo is None and current.tzinfo is not None:
            return current.tz_localize(None)

        if (
            latest.tzinfo is not None
            and current.tzinfo is not None
        ):
            return current.tz_convert(latest.tzinfo)

        return current

    def _refresh_due(self, frame, interval, now):
        if frame is None or frame.empty:
            return True

        interval_minutes = candle_interval_minutes(
            interval
        )
        current = self._normalise_now(now, frame)
        latest_start = pd.Timestamp(
            frame["date"].iloc[-1]
        )

        next_eligible = (
            latest_start
            + pd.Timedelta(
                minutes=2 * interval_minutes
            )
            + pd.Timedelta(
                seconds=self._completion_buffer_seconds
            )
        )

        return current >= next_eligible

    @staticmethod
    def _merge(existing, incoming, lookback_days):
        if existing is None or existing.empty:
            combined = incoming.copy()
        else:
            combined = pd.concat(
                [existing, incoming],
                ignore_index=True,
            )

        if combined.empty:
            return combined

        combined["date"] = pd.to_datetime(
            combined["date"]
        )
        combined = (
            combined.sort_values("date")
            .drop_duplicates(
                subset=["date"],
                keep="last",
            )
            .reset_index(drop=True)
        )

        latest = pd.Timestamp(
            combined["date"].iloc[-1]
        )
        cutoff = latest - pd.Timedelta(
            days=max(int(lookback_days), 1)
        )

        return combined[
            combined["date"] >= cutoff
        ].reset_index(drop=True)

    def get(
        self,
        kite,
        instrument_token,
        interval,
        *,
        lookback_days=5,
        now=None,
        require_advance=False,
        fetcher=None,
    ):
        key = self._key(
            instrument_token,
            interval,
        )
        existing = self._frames.get(key)

        if (
            existing is not None
            and not self._refresh_due(
                existing,
                interval,
                now,
            )
        ):
            self.cache_hits += 1
            return existing.copy()

        previous_latest = None

        if existing is not None and not existing.empty:
            previous_latest = pd.Timestamp(
                existing["date"].iloc[-1]
            )
            overlap = timedelta(
                minutes=candle_interval_minutes(
                    interval
                )
            )
            from_date = (
                previous_latest.to_pydatetime()
                - overlap
            )
        else:
            from_date = None

        effective_now = self._normalise_now(
            now or datetime.now(),
            existing,
        ).to_pydatetime()
        effective_fetcher = fetcher or self._fetcher
        incoming = effective_fetcher(
            kite,
            instrument_token,
            interval,
            lookback_days=lookback_days,
            from_date=from_date,
            to_date=effective_now,
            now=effective_now,
        )
        self.api_fetches += 1

        if incoming is None or incoming.empty:
            if require_advance:
                return pd.DataFrame()

            return (
                existing.copy()
                if existing is not None
                else pd.DataFrame()
            )

        merged = self._merge(
            existing,
            incoming,
            lookback_days,
        )
        self._frames[key] = merged

        if (
            require_advance
            and previous_latest is not None
            and pd.Timestamp(
                merged["date"].iloc[-1]
            ) <= previous_latest
        ):
            return pd.DataFrame()

        return merged.copy()

    def clear(self):
        self._frames.clear()
        self.api_fetches = 0
        self.cache_hits = 0


LIVE_CANDLE_CACHE = IncrementalCandleCache()
