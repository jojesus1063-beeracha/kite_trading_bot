import threading
import time

import pandas as pd

from matmon_scan_prefetch import prefetch_entry_candles


class Cache:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def get(self, kite, token, interval, **kwargs):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return pd.DataFrame({"date": [token]})


def test_prefetch_is_bounded_and_returns_every_symbol():
    cache = Cache()
    results = prefetch_entry_candles(
        object(),
        ["A", "B", "C", "D"],
        {"A": 1, "B": 2, "C": 3, "D": 4},
        interval="3minute",
        lookback_days=5,
        now=None,
        workers=3,
        cache=cache,
        fetcher=lambda *a, **k: None,
    )
    assert set(results) == {"A", "B", "C", "D"}
    assert 1 < cache.maximum <= 3
