"""
End-of-day watchlist high/low analytics: for each watchlist symbol,
finds the session's day high/low (with precise 1-minute timestamps),
computes range/percentage metrics, and persists a resumable snapshot.

Analytics/reporting only -- never touches entry, exit, position
sizing, stop-loss, target, ADX, market alignment, or scheduling.
Designed to run as a SEPARATE end-of-day report, not inside any
position-monitor/entry-scan cycle.

Reuses existing infrastructure rather than duplicating it:
  - fetch_candles() (data_feed.py) for all historical data, including
    its built-in retry/exponential-backoff -- no new fetch or retry
    code here at all.
  - get_instrument_token() (data_feed.py) for symbol->token mapping.
  - The same atomic-write pattern (temp file + fsync + os.replace)
    already used in trade_log.py.
"""
import json
import os
import time
import logging
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from data_feed import fetch_candles, get_instrument_token

logger = logging.getLogger("watchlist_range_analytics")

KOLKATA = ZoneInfo("Asia/Kolkata")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_daily_range.json")


def determine_session_date(kite, token, requested_date=None):
    """
    Returns the most recent completed trading session date <=
    requested_date (or <= today if not given), as a date object.
    Fetches ~10 days of DAILY candles -- since Kite's data only ever
    contains real trading days, the latest one at or before the
    target date IS the correct trading session, naturally handling
    weekends and market holidays without any separate calendar logic.
    Returns None if no trading-day data is found at all (fail-safe).
    """
    target = requested_date or datetime.now(KOLKATA).date()
    df = fetch_candles(kite, token, "day", lookback_days=15, trim_incomplete=False)
    if df.empty:
        return None
    df["session_date"] = df["date"].dt.date
    valid = df[df["session_date"] <= target]
    if valid.empty:
        return None
    return valid["session_date"].max()


def fetch_1min_candles_for_session(kite, token, session_date):
    """
    Fetches 1-minute candles for exactly ONE trading session, by
    reusing fetch_candles() (interval='minute', including its retry/
    backoff) with a lookback wide enough to cover session_date, then
    filtering the returned range down to that single date.
    """
    today = datetime.now(KOLKATA).date()
    lookback_days = max((today - session_date).days + 2, 2)
    df = fetch_candles(kite, token, "minute", lookback_days=lookback_days, trim_incomplete=False)
    if df.empty:
        return df
    df["session_date"] = df["date"].dt.date
    session_df = df[df["session_date"] == session_date].drop(columns=["session_date"])
    return session_df.reset_index(drop=True)


def get_previous_close(kite, token, session_date):
    """Fetches the daily candle for the trading day immediately before
    session_date. Returns None if unavailable (fail-safe, never 0)."""
    df = fetch_candles(kite, token, "day", lookback_days=15, trim_incomplete=False)
    if df.empty:
        return None
    df["session_date"] = df["date"].dt.date
    prior = df[df["session_date"] < session_date]
    if prior.empty:
        return None
    return float(prior.sort_values("session_date").iloc[-1]["close"])


def find_high_low_timestamps(df_1m):
    """
    Given 1-minute candles for one session, finds day_high/day_low and
    every candle matching each (handling repeated identical highs/lows
    correctly -- first and last are tracked separately, not assumed
    to be the same candle). Returns a dict with day_high, day_low, and
    the four timestamp fields (Asia/Kolkata, ISO format), or all None
    if df_1m is empty.
    """
    if df_1m.empty:
        return {"day_high": None, "day_low": None, "high_first_reached_at": None,
                "high_last_touched_at": None, "low_first_reached_at": None,
                "low_last_touched_at": None, "high_volume": None, "low_volume": None}

    day_high = df_1m["high"].max()
    day_low = df_1m["low"].min()

    high_matches = df_1m[df_1m["high"] == day_high]
    low_matches = df_1m[df_1m["low"] == day_low]

    def to_iso(ts):
        if ts.tzinfo is None:
            ts = ts.tz_localize(KOLKATA)
        else:
            ts = ts.tz_convert(KOLKATA)
        return ts.isoformat()

    return {
        "day_high": float(day_high), "day_low": float(day_low),
        "high_first_reached_at": to_iso(high_matches.iloc[0]["date"]),
        "high_last_touched_at": to_iso(high_matches.iloc[-1]["date"]),
        "low_first_reached_at": to_iso(low_matches.iloc[0]["date"]),
        "low_last_touched_at": to_iso(low_matches.iloc[-1]["date"]),
        "high_volume": float(high_matches.iloc[0]["volume"]) if "volume" in high_matches.columns else None,
        "low_volume": float(low_matches.iloc[0]["volume"]) if "volume" in low_matches.columns else None,
    }


def save_watchlist_snapshot(data, output_path=None):
    """
    Atomic write: temp file in the same directory, flush, fsync,
    os.replace(). Exception-safe (temp file removed on any failure).
    Same pattern as trade_log.py's save_bot_status().
    """
    from json_safe import json_safe
    path = output_path if output_path is not None else OUTPUT_PATH
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(json_safe(data), f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def load_watchlist_snapshot(output_path=None):
    path = output_path if output_path is not None else OUTPUT_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def compute_symbol_range_analytics(kite, symbol, exchange, session_date=None):
    """
    Full per-symbol computation. Never raises -- any failure results
    in a status-tagged result (API_ERROR/NO_DATA/INVALID_CANDLES)
    rather than an exception, so one bad symbol can never stop the
    rest of the watchlist. Never substitutes zero for missing data --
    every percentage is None when its inputs aren't available.
    """
    base_result = {"symbol": symbol, "exchange": exchange, "session_date": None,
                   "day_open": None, "day_low": None, "low_first_reached_at": None,
                   "low_last_touched_at": None, "day_high": None, "high_first_reached_at": None,
                   "high_last_touched_at": None, "previous_close": None, "close_price": None,
                   "intraday_range_inr": None, "intraday_range_pct": None, "low_to_high_pct": None,
                   "open_to_high_pct": None, "previous_close_to_high_pct": None, "close_change_pct": None,
                   "distance_below_high_pct": None, "distance_above_low_pct": None,
                   "high_volume": None, "low_volume": None, "status": None, "error": None}
    try:
        token = get_instrument_token(kite, symbol, exchange)
    except Exception as e:
        return {**base_result, "status": "API_ERROR", "error": f"instrument token lookup failed: {e}"}

    try:
        resolved_date = determine_session_date(kite, token, requested_date=session_date)
    except Exception as e:
        return {**base_result, "status": "API_ERROR", "error": f"session date lookup failed: {e}"}

    if resolved_date is None:
        return {**base_result, "status": "API_ERROR", "error": "no trading-day data available at all"}

    base_result["session_date"] = str(resolved_date)

    try:
        df_1m = fetch_1min_candles_for_session(kite, token, resolved_date)
    except Exception as e:
        return {**base_result, "status": "API_ERROR", "error": f"1-minute candle fetch failed: {e}"}

    if df_1m.empty:
        return {**base_result, "status": "NO_DATA", "error": "no 1-minute candles for this session"}

    hl = find_high_low_timestamps(df_1m)
    day_high, day_low = hl["day_high"], hl["day_low"]

    if day_high is None or day_low is None or day_high <= 0 or day_low <= 0 or day_high < day_low:
        return {**base_result, "status": "INVALID_CANDLES",
                "error": f"nonsensical high/low values: high={day_high}, low={day_low}"}

    day_open = float(df_1m.iloc[0]["open"])
    close_price = float(df_1m.iloc[-1]["close"])

    try:
        previous_close = get_previous_close(kite, token, resolved_date)
    except Exception as e:
        previous_close = None
        logger.warning(f"{symbol}: previous close lookup failed: {e}")

    intraday_range_inr = day_high - day_low
    low_to_high_pct = (intraday_range_inr / day_low * 100) if day_low > 0 else None
    open_to_high_pct = ((day_high - day_open) / day_open * 100) if day_open > 0 else None
    previous_close_to_high_pct = ((day_high - previous_close) / previous_close * 100) if previous_close and previous_close > 0 else None
    close_change_pct = ((close_price - previous_close) / previous_close * 100) if previous_close and previous_close > 0 else None
    distance_below_high_pct = ((day_high - close_price) / day_high * 100) if day_high > 0 else None
    distance_above_low_pct = ((close_price - day_low) / day_low * 100) if day_low > 0 else None

    status = "COMPLETE" if previous_close is not None else "PARTIAL"
    error = None if previous_close is not None else "previous close unavailable"

    return {
        **base_result,
        "day_open": day_open, "day_low": day_low,
        "low_first_reached_at": hl["low_first_reached_at"], "low_last_touched_at": hl["low_last_touched_at"],
        "day_high": day_high,
        "high_first_reached_at": hl["high_first_reached_at"], "high_last_touched_at": hl["high_last_touched_at"],
        "previous_close": previous_close, "close_price": close_price,
        "intraday_range_inr": intraday_range_inr, "intraday_range_pct": low_to_high_pct,
        "low_to_high_pct": low_to_high_pct, "open_to_high_pct": open_to_high_pct,
        "previous_close_to_high_pct": previous_close_to_high_pct, "close_change_pct": close_change_pct,
        "distance_below_high_pct": distance_below_high_pct, "distance_above_low_pct": distance_above_low_pct,
        "high_volume": hl["high_volume"], "low_volume": hl["low_volume"],
        "status": status, "error": error,
    }


def _build_snapshot_payload(results_by_symbol, watchlist, session_date_str=None):
    import uuid
    symbols_list = [results_by_symbol[e["symbol"]] for e in watchlist if e["symbol"] in results_by_symbol]
    complete_count = sum(1 for r in symbols_list if r["status"] == "COMPLETE")
    error_count = sum(1 for r in symbols_list if r["status"] in ("API_ERROR", "INVALID_CANDLES", "NO_DATA"))
    return {
        "schema_version": 1,
        "snapshot_id": str(uuid.uuid4()),
        "generated_at": datetime.now(KOLKATA).isoformat(),
        "session_date": session_date_str,
        "watchlist_size": len(watchlist),
        "processed_count": len(symbols_list),
        "complete_count": complete_count,
        "error_count": error_count,
        "symbols": symbols_list,
    }


def process_watchlist_range_analytics(watchlist, kite, output_path=None, session_date=None):
    """
    Processes every symbol in `watchlist` (list of {"symbol", "exchange"}
    dicts). RESUMES from any existing snapshot at output_path: symbols
    already COMPLETE there are skipped, not re-fetched -- an
    interrupted run resumes instead of restarting from scratch. Saves
    progress (atomic write) after EVERY symbol, successful or not, so
    a crash mid-run loses at most the single symbol in flight. One
    symbol's failure never stops the rest -- compute_symbol_range_
    analytics() never raises. Sequential processing only (no parallel
    requests), per the explicit rate-limit requirement.
    """
    existing = load_watchlist_snapshot(output_path)
    results_by_symbol = {}
    if existing and existing.get("symbols"):
        for r in existing["symbols"]:
            if r.get("status") == "COMPLETE":
                results_by_symbol[r["symbol"]] = r
        logger.info(f"Resuming watchlist range analytics: {len(results_by_symbol)} symbol(s) "
                    f"already COMPLETE from a previous run, skipping those")

    session_date_str = str(session_date) if session_date else None
    processed_this_run = 0
    for entry in watchlist:
        symbol = entry["symbol"]
        exchange = entry.get("exchange", "NSE")
        if symbol in results_by_symbol:
            continue

        try:
            result = compute_symbol_range_analytics(kite, symbol, exchange, session_date=session_date)
        except Exception as e:
            logger.error(f"{symbol}: unexpected error not caught internally, treating as API_ERROR: {e}")
            result = {"symbol": symbol, "exchange": exchange, "status": "API_ERROR", "error": str(e)}

        results_by_symbol[symbol] = result
        if result.get("session_date"):
            session_date_str = result["session_date"]
        processed_this_run += 1

        payload = _build_snapshot_payload(results_by_symbol, watchlist, session_date_str)
        save_watchlist_snapshot(payload, output_path)
        logger.info(f"Watchlist range analytics: {symbol} -> {result['status']} "
                    f"({payload['processed_count']}/{payload['watchlist_size']} processed)")

    logger.info(f"Watchlist range analytics complete: {processed_this_run} symbol(s) processed this run, "
                f"{len(results_by_symbol)}/{len(watchlist)} total")
    return load_watchlist_snapshot(output_path)
