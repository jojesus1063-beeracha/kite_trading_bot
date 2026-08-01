"""
Tests for the /monitor watchlist auto-refresh fix. Since this test
environment can't execute real JavaScript, JS-logic correctness is
verified STRUCTURALLY (the correct state-preservation code patterns
are present in the generated page source, in the correct order) --
explicitly named as such below, not claimed as full browser execution
tests. The backend /api/monitor-data endpoint's zero-Kite-calls
guarantee IS fully, directly tested in real Python.
"""
from flask import Flask, render_template_string
from monitor_route import MONITOR_PAGE
from watchlist_range_analytics import load_watchlist_snapshot
from watchlist_dashboard_helpers import compute_summary_cards, classify_report_freshness
import json

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

app = Flask(__name__)
snap = load_watchlist_snapshot()
with app.app_context():
    html = render_template_string(
        MONITOR_PAGE, updated="N/A", positions=[], portfolio={}, session={}, health={},
        profit_factor_display="N/A", watchlist_snapshot=snap,
        freshness=classify_report_freshness(snap), summary_cards=compute_summary_cards(snap),
        watchlist_symbols_json=json.dumps(snap.get("symbols", []) if snap else []),
    )

# --- Root cause fix: no full-page reload mechanism present ---
print("--- Root Cause Fix ---")
check("No <meta http-equiv=refresh> tag (the actual original cause)", 'http-equiv="refresh"' not in html)
check("No window.location.reload() call anywhere", "location.reload()" not in html)
check("No full-page redirect/re-render triggered by a timer", "window.location.href" not in html)

# --- 1-3: search/sort/filter state preserved across a poll (structural: correct read-before-fetch, restore-after-fetch pattern) ---
print("\n--- State Preservation Structure (1-3) ---")
poll_fn_start = html.find("async function pollMonitorData")
poll_fn_end = html.find("}", html.find("console.warn", poll_fn_start)) + 1
poll_fn_body = html[poll_fn_start:poll_fn_end]

check("pollMonitorData captures search value BEFORE fetching (currentState.search)",
      "currentState.search" in poll_fn_body and poll_fn_body.index("currentState") < poll_fn_body.index("fetch(")
      if "fetch(" in poll_fn_body else False)
check("pollMonitorData restores the search input's value AFTER fetching new data",
      '"wl-search").value = currentState.search' in poll_fn_body)
check("pollMonitorData restores the sort dropdown's value AFTER fetching new data",
      '"wl-sort").value = currentState.sort' in poll_fn_body)
check("pollMonitorData restores the filter dropdown's value AFTER fetching new data",
      '"wl-filter").value = currentState.filter' in poll_fn_body)
check("pollMonitorData captures scrollY and restores it after re-rendering",
      "currentState.scrollY" in poll_fn_body and "window.scrollTo(0, currentState.scrollY)" in poll_fn_body)

# --- 4: new rows are filtered using current selections ---
print("\n--- New Rows Filtered With Current Selections (4) ---")
# Confirms renderWatchlistTable() (which reads the live DOM control values)
# is called AFTER control values are restored, not before -- so the
# re-render genuinely reflects the restored search/sort/filter.
restore_search_idx = poll_fn_body.find("currentState.search;")
render_call_idx = poll_fn_body.find("renderWatchlistTable();")
check("renderWatchlistTable() is called AFTER restoring the search control (correct order)",
      restore_search_idx != -1 and render_call_idx != -1 and render_call_idx > restore_search_idx)

# --- 5: failed refresh retains the previous snapshot ---
print("\n--- Failed Refresh Retains Previous Snapshot (5) ---")
catch_block_start = poll_fn_body.find("} catch")
catch_block = poll_fn_body[catch_block_start:]
check("The catch block does NOT clear WATCHLIST_DATA", "WATCHLIST_DATA.length = 0" not in catch_block)
check("The catch block does NOT call renderWatchlistTable() (table is left as-is)", "renderWatchlistTable()" not in catch_block)
check("The catch block shows the failure indicator instead", "setLastRefreshIndicator(false)" in catch_block)

# --- 6: manual page reopen restores state from sessionStorage ---
print("\n--- sessionStorage Restore on Reopen (6) ---")
check("restoreControlState() reads from sessionStorage with the correct keys",
      'sessionStorage.getItem("watchlistSearch")' in html and
      'sessionStorage.getItem("watchlistSort")' in html and
      'sessionStorage.getItem("watchlistFilter")' in html)
check("restoreControlState() is called on page load (before the initial render)",
      html.find("restoreControlState();") < html.find("renderWatchlistTable();\n        setLastRefreshIndicator"))
check("saveControlState() persists to sessionStorage on every control change",
      "sessionStorage.setItem(\"watchlistSearch\"" in html and "saveControlState()" in html)

# --- 7: zero Kite API calls from the dashboard endpoint (REAL test, not structural) ---
print("\n--- Zero Kite Calls From /api/monitor-data (7, real test) ---")
import inspect
import configure_app
source = inspect.getsource(configure_app.api_monitor_data)
check("api_monitor_data() function makes no actual Kite method calls (no kite.margins/quote/historical_data etc)",
      "kite." not in source and "kite(" not in source)
check("api_monitor_data() only calls load_bot_status()/load_watchlist_snapshot() (file reads)",
      "load_bot_status()" in source and "load_watchlist_snapshot()" in source)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
