"""
UI-presentation layer for the watchlist day-range report. Reads and
summarizes watchlist_daily_range.json ONLY -- never calls Kite, never
touches watchlist_range_analytics.py's calculations. Pure functions
operating on already-computed data, so every number shown in the
dashboard is provably derived from the same verified JSON, not
recalculated differently in a second place.
"""
import statistics
from datetime import datetime, date, timedelta
from collections import Counter

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

KOLKATA = ZoneInfo("Asia/Kolkata")


def _valid_symbols(snapshot):
    """Only COMPLETE/PARTIAL rows carry real numbers -- NO_DATA/API_ERROR/
    INVALID_CANDLES rows are excluded from every summary calculation,
    same discipline as the live-position dashboard's INVALID_DATA handling."""
    if not snapshot or not snapshot.get("symbols"):
        return []
    return [s for s in snapshot["symbols"] if s.get("status") in ("COMPLETE", "PARTIAL")
            and s.get("low_to_high_pct") is not None]


def _extract_hour(iso_timestamp):
    """Returns the hour (0-23, IST) from an ISO timestamp string, or None."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.hour
    except (ValueError, TypeError):
        return None


def compute_summary_cards(snapshot):
    """
    Returns the summary-card data: largest mover, strongest/weakest
    close, average/median range, most common hour for highs/lows,
    and a high-retest count. Every value is None (not fabricated)
    when there's no valid data to compute it from.
    """
    valid = _valid_symbols(snapshot)
    if not valid:
        return {
            "largest_mover": None, "strongest_close": None, "weakest_close": None,
            "average_range_pct": None, "median_range_pct": None,
            "most_common_high_hour": None, "most_common_low_hour": None,
            "high_retest_count": 0, "low_retest_count": 0,
        }

    largest_mover = max(valid, key=lambda s: s["low_to_high_pct"])

    close_change_valid = [s for s in valid if s.get("close_change_pct") is not None]
    strongest_close = max(close_change_valid, key=lambda s: s["close_change_pct"]) if close_change_valid else None
    weakest_close = min(close_change_valid, key=lambda s: s["close_change_pct"]) if close_change_valid else None

    ranges = [s["low_to_high_pct"] for s in valid]
    average_range_pct = sum(ranges) / len(ranges)
    median_range_pct = statistics.median(ranges)

    high_hours = [h for h in (_extract_hour(s.get("high_first_reached_at")) for s in valid) if h is not None]
    low_hours = [h for h in (_extract_hour(s.get("low_first_reached_at")) for s in valid) if h is not None]
    most_common_high_hour = Counter(high_hours).most_common(1)[0][0] if high_hours else None
    most_common_low_hour = Counter(low_hours).most_common(1)[0][0] if low_hours else None

    high_retest_count = sum(1 for s in valid
                            if s.get("high_first_reached_at") and s.get("high_last_touched_at")
                            and s["high_first_reached_at"] != s["high_last_touched_at"])
    low_retest_count = sum(1 for s in valid
                           if s.get("low_first_reached_at") and s.get("low_last_touched_at")
                           and s["low_first_reached_at"] != s["low_last_touched_at"])

    return {
        "largest_mover": largest_mover, "strongest_close": strongest_close, "weakest_close": weakest_close,
        "average_range_pct": average_range_pct, "median_range_pct": median_range_pct,
        "most_common_high_hour": most_common_high_hour, "most_common_low_hour": most_common_low_hour,
        "high_retest_count": high_retest_count, "low_retest_count": low_retest_count,
    }


def _expected_latest_session_date(now):
    """
    Calendar-based estimate of what the latest trading session SHOULD
    be -- deliberately does NOT call Kite (the dashboard must never
    make API calls), so this can't know about specific market
    holidays. Weekday/weekend + market-open-time approximation only,
    which matches the explicit spec examples (Saturday showing Friday
    is correct; Monday after close still showing Friday is stale).
    """
    today = now.date()
    weekday = now.weekday()  # 0=Monday .. 6=Sunday
    if weekday >= 5:  # Saturday or Sunday
        days_back = weekday - 4  # Saturday(5)->1, Sunday(6)->2 -> lands on Friday
        return today - timedelta(days=days_back)
    is_before_market_open = (now.hour, now.minute) < (9, 15)
    if is_before_market_open:
        days_back = 3 if weekday == 0 else 1  # Monday before open -> previous Friday
        return today - timedelta(days=days_back)
    return today


def classify_report_freshness(snapshot, now=None):
    """
    Returns dict with status in {NO_REPORT_AVAILABLE, REPORT_ERROR,
    REPORT_PROCESSING, REPORT_STALE, REPORT_PARTIAL, REPORT_READY},
    plus a human-readable reason and both the report's and expected
    session dates for display.
    """
    if now is None:
        now = datetime.now(KOLKATA)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=KOLKATA)

    if snapshot is None:
        return {"status": "NO_REPORT_AVAILABLE", "reason": "no report file found",
                "report_session_date": None, "expected_session_date": None}

    report_session_str = snapshot.get("session_date")
    if not report_session_str:
        return {"status": "REPORT_ERROR", "reason": "report has no session_date",
                "report_session_date": None, "expected_session_date": None}
    try:
        report_session_date = date.fromisoformat(report_session_str)
    except ValueError:
        return {"status": "REPORT_ERROR", "reason": "session_date is unparseable",
                "report_session_date": report_session_str, "expected_session_date": None}

    expected = _expected_latest_session_date(now)
    error_count = snapshot.get("error_count", 0)
    processed_count = snapshot.get("processed_count", 0)
    watchlist_size = snapshot.get("watchlist_size", 0)

    if processed_count < watchlist_size:
        return {"status": "REPORT_PROCESSING",
                "reason": f"{processed_count}/{watchlist_size} symbols processed so far",
                "report_session_date": report_session_str, "expected_session_date": str(expected)}

    if report_session_date < expected:
        return {"status": "REPORT_STALE",
                "reason": f"report is for {report_session_date}, expected latest session is {expected}",
                "report_session_date": report_session_str, "expected_session_date": str(expected)}

    if error_count > 0:
        return {"status": "REPORT_PARTIAL", "reason": f"{error_count} symbol(s) failed",
                "report_session_date": report_session_str, "expected_session_date": str(expected)}

    return {"status": "REPORT_READY", "reason": "up to date",
            "report_session_date": report_session_str, "expected_session_date": str(expected)}
