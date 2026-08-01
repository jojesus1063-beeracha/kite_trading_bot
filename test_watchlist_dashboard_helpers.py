from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from watchlist_dashboard_helpers import compute_summary_cards, classify_report_freshness

passed, failed = 0, 0
KOLKATA = ZoneInfo("Asia/Kolkata")

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

# --- compute_summary_cards ---
print("--- Summary Cards ---")
sample_snapshot = {
    "symbols": [
        {"symbol": "A", "status": "COMPLETE", "low_to_high_pct": 5.0, "close_change_pct": 2.0,
         "high_first_reached_at": "2026-07-31T09:15:00+05:30", "high_last_touched_at": "2026-07-31T09:15:00+05:30",
         "low_first_reached_at": "2026-07-31T10:00:00+05:30", "low_last_touched_at": "2026-07-31T10:00:00+05:30"},
        {"symbol": "B", "status": "COMPLETE", "low_to_high_pct": 9.94, "close_change_pct": -3.5,
         "high_first_reached_at": "2026-07-31T14:00:00+05:30", "high_last_touched_at": "2026-07-31T14:05:00+05:30",
         "low_first_reached_at": "2026-07-31T09:20:00+05:30", "low_last_touched_at": "2026-07-31T09:20:00+05:30"},
        {"symbol": "C", "status": "COMPLETE", "low_to_high_pct": 3.0, "close_change_pct": 8.32,
         "high_first_reached_at": "2026-07-31T14:30:00+05:30", "high_last_touched_at": "2026-07-31T15:29:00+05:30",
         "low_first_reached_at": "2026-07-31T09:15:00+05:30", "low_last_touched_at": "2026-07-31T09:15:00+05:30"},
        {"symbol": "D", "status": "API_ERROR", "low_to_high_pct": None},  # excluded from all calcs
    ],
}
cards = compute_summary_cards(sample_snapshot)
check("Largest mover correctly identified (B, 9.94%)", cards["largest_mover"]["symbol"] == "B")
check("Strongest close correctly identified (C, +8.32%)", cards["strongest_close"]["symbol"] == "C")
check("Weakest close correctly identified (B, -3.5%)", cards["weakest_close"]["symbol"] == "B")
check("Average range correct ((5.0+9.94+3.0)/3)", abs(cards["average_range_pct"] - (5.0+9.94+3.0)/3) < 0.001)
check("Median range correct (5.0)", cards["median_range_pct"] == 5.0)
check("Most common high hour correctly identified (14:xx appears twice)", cards["most_common_high_hour"] == 14)
check("Most common low hour correctly identified (09:xx appears three times)", cards["most_common_low_hour"] == 9)
check("High retest count correct (B and C both have first != last)", cards["high_retest_count"] == 2)
check("Low retest count correct (none have first != last)", cards["low_retest_count"] == 0)
check("Invalid/error symbol (D) excluded from all calculations", cards["largest_mover"]["symbol"] != "D")

empty_cards = compute_summary_cards({"symbols": []})
check("Empty snapshot: all fields None/0, no crash", empty_cards["largest_mover"] is None and empty_cards["high_retest_count"] == 0)
check("None snapshot: no crash", compute_summary_cards(None)["largest_mover"] is None)

# --- classify_report_freshness ---
print("\n--- Report Freshness ---")

# NO_REPORT_AVAILABLE
check("None snapshot -> NO_REPORT_AVAILABLE", classify_report_freshness(None)["status"] == "NO_REPORT_AVAILABLE")

# Saturday showing Friday's session -- correct, should be READY (matches the spec's own example)
saturday = datetime(2026, 8, 1, 12, 0, tzinfo=KOLKATA)  # Saturday
friday_report = {"session_date": "2026-07-31", "processed_count": 108, "watchlist_size": 108, "error_count": 0}
result_sat = classify_report_freshness(friday_report, now=saturday)
check("Saturday showing Friday's report -> REPORT_READY, not stale (matches explicit spec example)",
      result_sat["status"] == "REPORT_READY")

# Monday after market close, file STILL shows Friday -- should be STALE
monday_evening = datetime(2026, 8, 3, 16, 0, tzinfo=KOLKATA)  # Monday, after close
result_mon_stale = classify_report_freshness(friday_report, now=monday_evening)
check("Monday evening, report still shows Friday -> REPORT_STALE (matches explicit spec example)",
      result_mon_stale["status"] == "REPORT_STALE")

# Monday after close, file correctly shows Monday -- READY
monday_report = {"session_date": "2026-08-03", "processed_count": 108, "watchlist_size": 108, "error_count": 0}
result_mon_ready = classify_report_freshness(monday_report, now=monday_evening)
check("Monday evening, report correctly shows Monday -> REPORT_READY", result_mon_ready["status"] == "REPORT_READY")

# Partial processing
partial_report = {"session_date": "2026-07-31", "processed_count": 50, "watchlist_size": 108, "error_count": 0}
result_processing = classify_report_freshness(partial_report, now=saturday)
check("Fewer processed than watchlist size -> REPORT_PROCESSING", result_processing["status"] == "REPORT_PROCESSING")

# Errors present but otherwise complete and current
error_report = {"session_date": "2026-07-31", "processed_count": 108, "watchlist_size": 108, "error_count": 2}
result_partial = classify_report_freshness(error_report, now=saturday)
check("Complete processing but some errors -> REPORT_PARTIAL", result_partial["status"] == "REPORT_PARTIAL")

# Missing/bad session_date
check("Missing session_date -> REPORT_ERROR",
      classify_report_freshness({"processed_count": 1, "watchlist_size": 1}, now=saturday)["status"] == "REPORT_ERROR")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
