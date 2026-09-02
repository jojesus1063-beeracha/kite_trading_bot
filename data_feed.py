"""
Historical candle data via Kite Connect's REST API.

For the live loop, main.py polls historical_data() for the most recent
candles every few minutes rather than aggregating ticks from the
WebSocket — this is simpler and plenty fast enough for a 5-min-entry
strategy. If you later want tick-level precision (e.g. faster stop-loss
reaction), swap this for KiteTicker and aggregate candles yourself.
"""

import time
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from api_rate_limiter import HISTORICAL_API_LIMITER
from scheduler import candle_interval_minutes

_INSTRUMENT_TICK_SIZES = {}
_INSTRUMENT_MASTER_CACHE = {}

INSTRUMENT_MASTER_CACHE_DIR = Path(
    "runtime/instrument_master_cache"
)


def _instrument_cache_path(exchange: str) -> Path:
    """Return today's exchange instrument-master cache path in IST."""
    day = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).date().isoformat()

    return (
        INSTRUMENT_MASTER_CACHE_DIR
        / day
        / f"{str(exchange).upper()}.json"
    )


def _read_instrument_master_disk_cache(exchange: str):
    path = _instrument_cache_path(exchange)

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            records = json.load(f)

        if not isinstance(records, list) or not records:
            return None

        mapped = {
            inst["tradingsymbol"]: inst
            for inst in records
            if isinstance(inst, dict)
            and inst.get("tradingsymbol")
            and inst.get("instrument_token") is not None
        }

        return mapped or None

    except Exception:
        # A corrupt/malformed local cache must never fabricate
        # instrument metadata. Fall through to the broker API.
        return None


def _write_instrument_master_disk_cache(
    exchange: str,
    instruments,
) -> None:
    path = _instrument_cache_path(exchange)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".json.tmp")

    # Only persist fields used by this bot. This also avoids
    # serialization problems from date/datetime values such as expiry.
    records = []

    for inst in instruments:
        if not isinstance(inst, dict):
            continue

        symbol = inst.get("tradingsymbol")
        token = inst.get("instrument_token")

        if not symbol or token is None:
            continue

        records.append(
            {
                "tradingsymbol": symbol,
                "instrument_token": int(token),
                "tick_size": inst.get("tick_size"),
                "name": inst.get("name"),
                "exchange": inst.get("exchange", exchange),
                "segment": inst.get("segment"),
                "instrument_type": inst.get("instrument_type"),
                "lot_size": inst.get("lot_size"),
            }
        )

    if not records:
        raise ValueError(
            f"Refusing to cache empty instrument master for {exchange}"
        )

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, separators=(",", ":"))

    tmp_path.replace(path)


def _get_instrument_master(kite, exchange: str):
    exchange = str(exchange).upper()

    # Level 1: same-process memory cache.
    if exchange in _INSTRUMENT_MASTER_CACHE:
        return _INSTRUMENT_MASTER_CACHE[exchange]

    # Level 2: today's persistent local cache.
    disk_cached = _read_instrument_master_disk_cache(exchange)

    if disk_cached is not None:
        _INSTRUMENT_MASTER_CACHE[exchange] = disk_cached
        return disk_cached

    # Level 3: broker request. Only reached once per exchange/day
    # when no valid local cache exists.
    instruments = kite.instruments(exchange)

    mapped = {
        inst["tradingsymbol"]: inst
        for inst in instruments
        if inst.get("tradingsymbol")
    }

    if not mapped:
        raise ValueError(
            f"Empty instrument master returned for {exchange}"
        )

    # Persist before exposing it to subsequent restarts.
    _write_instrument_master_disk_cache(
        exchange,
        instruments,
    )

    _INSTRUMENT_MASTER_CACHE[exchange] = mapped
    return mapped


def get_instrument_token(kite, symbol: str, exchange: str) -> int:
    instruments = _get_instrument_master(kite, exchange)

    inst = instruments.get(symbol)

    if inst is None:
        raise ValueError(
            f"Instrument token not found for {exchange}:{symbol}"
        )

    tick_size = inst.get("tick_size")

    if tick_size is not None and float(tick_size) > 0:
        _INSTRUMENT_TICK_SIZES[(exchange, symbol)] = float(
            tick_size
        )

    return int(inst["instrument_token"])

def get_cached_instrument_tick_size(
    symbol: str,
    exchange: str,
    default: float = 0.05,
) -> float:
    """Return tick size captured during the startup instrument lookup."""
    return float(
        _INSTRUMENT_TICK_SIZES.get(
            (exchange, symbol),
            default,
        )
    )

def get_company_name(kite, symbol: str, exchange: str) -> str:
    try:
        instruments = _get_instrument_master(kite, exchange)
        inst = instruments.get(symbol)

        if inst:
            return inst.get("name") or symbol
    except Exception:
        pass

    return symbol

def trim_incomplete_candles(df, interval_minutes, buffer_seconds=10, now=None):
    """
    Removes any trailing candle(s) that have NOT fully finished forming
    yet. Kite returns the currently-forming candle as a live-updating
    row in historical_data() -- e.g. a 15-min candle that started 4
    minutes ago already shows real (but unstable, still-changing)
    OHLC. Using such a row as "the latest completed candle" means
    trend/signal decisions are made against noisy, evolving data
    rather than a genuinely closed bar.

    A candle starting at Wed Jul 29 10:33:59 IST 2026 covers [date, date + interval_minutes).
    It is only eligible once that full interval has elapsed, plus a
    small safety buffer (default 10s) to protect against querying
    right at the boundary before the broker has finalized the candle.
    """
    if df is None or df.empty:
        return df
    if now is None:
        now = pd.Timestamp.now(tz=df["date"].dt.tz)
    else:
        now = pd.Timestamp(now)
        candle_timezone = df["date"].dt.tz

        if candle_timezone is not None and now.tzinfo is None:
            now = now.tz_localize(candle_timezone)
        elif candle_timezone is None and now.tzinfo is not None:
            now = now.tz_localize(None)
        elif candle_timezone is not None and now.tzinfo is not None:
            now = now.tz_convert(candle_timezone)
    candle_end = df["date"] + pd.Timedelta(minutes=interval_minutes)
    eligible_at = candle_end + pd.Timedelta(seconds=buffer_seconds)
    return df[eligible_at <= now].reset_index(drop=True)


def fetch_candles(
    kite,
    instrument_token: int,
    interval: str,
    lookback_days: int = 5,
    max_retries: int = 3,
    trim_incomplete: bool = True,
    *,
    from_date=None,
    to_date=None,
    now=None,
    rate_limiter=HISTORICAL_API_LIMITER,
) -> pd.DataFrame:
    """
    Fetch recent historical candles.
    interval: Kite's interval strings, e.g. "3minute", "15minute".

    Retries with exponential backoff on transient failures (e.g. Kite's
    rate limiting) instead of letting the exception crash the whole bot.
    """
    effective_to_date = to_date or datetime.now()
    effective_from_date = (
        from_date
        if from_date is not None
        else effective_to_date - timedelta(days=lookback_days)
    )

    last_exc = None
    for attempt in range(max_retries):
        try:
            if rate_limiter is not None:
                rate_limiter.wait()

            data = kite.historical_data(
                instrument_token,
                effective_from_date,
                effective_to_date,
                interval,
            )
            df = pd.DataFrame(data)
            if df.empty:
                return df
            df["date"] = pd.to_datetime(df["date"])
            df = df[["date", "open", "high", "low", "close", "volume"]]
            if trim_incomplete:
                df = trim_incomplete_candles(
                    df,
                    candle_interval_minutes(interval),
                    now=now,
                )
            return df
        except Exception as e:
            last_exc = e
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)

    # All retries exhausted -- return an empty frame so the caller's
    # existing "if df.empty: continue" logic handles it safely instead
    # of crashing the whole process.
    print(f"fetch_candles: giving up after {max_retries} attempts: {last_exc}")
    return pd.DataFrame()
