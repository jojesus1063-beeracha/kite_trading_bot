"""Rate-conscious cache for completed Kite one-minute candles."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fno_bot.strategies.intraday_momentum import MinuteCandle

IST = ZoneInfo("Asia/Kolkata")


class HistoricalCandleCache:
    def __init__(self, kite, ttl_seconds: float = 55.0, clock_fn=None):
        self.kite = kite
        self.ttl_seconds = ttl_seconds
        self.clock_fn = clock_fn or (lambda: datetime.now(IST))
        self._cache = {}

    def completed_minute_candles(self, instrument_token: int, now=None):
        now = now or self.clock_fn()
        key = (instrument_token, now.date())
        cached = self._cache.get(key)
        if cached and (now - cached[0]).total_seconds() < self.ttl_seconds:
            return cached[1]

        session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        completed_before = now.replace(second=0, microsecond=0)
        if completed_before <= session_start:
            return []
        rows = self.kite.historical_data(
            instrument_token,
            session_start,
            completed_before - timedelta(microseconds=1),
            "minute",
            continuous=False,
            oi=False,
        )
        candles = []
        for row in rows:
            timestamp = row.get("date")
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if timestamp is not None and timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=IST)
            if timestamp is None or timestamp >= completed_before:
                continue
            candles.append(MinuteCandle(
                timestamp=timestamp,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            ))
        self._cache[key] = (now, candles)
        return candles
