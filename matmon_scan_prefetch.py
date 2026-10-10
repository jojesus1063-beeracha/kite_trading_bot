"""Bounded read-only candle prefetch for Matmon's large watchlist."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from candle_cache import LIVE_CANDLE_CACHE
from data_feed import fetch_candles

logger = logging.getLogger("matmon_scan_prefetch")


def prefetch_entry_candles(kite, symbols, tokens, *, interval, lookback_days,
                           now, workers=3, cache=LIVE_CANDLE_CACHE,
                           fetcher=fetch_candles):
    """Warm the existing cache; failures remain ordinary empty-data results."""

    work = [(symbol, tokens.get(symbol)) for symbol in symbols]
    work = [(symbol, token) for symbol, token in work if token is not None]
    results = {}

    def load(item):
        symbol, token = item
        frame = cache.get(
            kite,
            token,
            interval,
            lookback_days=lookback_days,
            now=now,
            require_advance=True,
            fetcher=fetcher,
        )
        return symbol, frame

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 3))) as pool:
        futures = [pool.submit(load, item) for item in work]
        for future in as_completed(futures):
            try:
                symbol, frame = future.result()
                results[symbol] = frame
            except Exception as exc:
                logger.warning("MATMON PREFETCH FAILED | %s", exc)

    return results
