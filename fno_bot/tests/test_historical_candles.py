from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.market_data.historical_candles import HistoricalCandleCache

IST = ZoneInfo("Asia/Kolkata")


class FakeKite:
    def __init__(self):
        self.calls = 0

    def historical_data(self, *args, **kwargs):
        self.calls += 1
        return [
            {"date": datetime(2026, 8, 26, 9, 15, tzinfo=IST), "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            {"date": datetime(2026, 8, 26, 10, 0, tzinfo=IST), "open": 2, "high": 3, "low": 2, "close": 3, "volume": 20},
        ]


def test_cache_excludes_forming_minute_and_reuses_result():
    kite = FakeKite()
    now = datetime(2026, 8, 26, 10, 0, 30, tzinfo=IST)
    cache = HistoricalCandleCache(kite, clock_fn=lambda: now)
    first = cache.completed_minute_candles(123)
    second = cache.completed_minute_candles(123)
    assert len(first) == 1
    assert second == first
    assert kite.calls == 1

