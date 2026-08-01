import os
import pandas as pd
from datetime import datetime, date
from unittest.mock import MagicMock, patch
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from watchlist_range_analytics import (
    find_high_low_timestamps, determine_session_date, fetch_1min_candles_for_session,
    get_previous_close, compute_symbol_range_analytics, process_watchlist_range_analytics,
    save_watchlist_snapshot, load_watchlist_snapshot,
)
from test_helpers import isolated_runtime_paths

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

# --- find_high_low_timestamps: repeated highs/lows, first vs last ---
print("--- High/Low Timestamp Extraction ---")
candle_times = pd.date_range("2026-07-31 09:15", periods=5, freq="1min", tz=KOLKATA)
df_repeated = pd.DataFrame({
    "date": candle_times,
    "open": [100, 98, 105, 103, 107], "high": [100, 110, 108, 110, 109],
    "low": [95, 97, 100, 101, 95], "close": [98, 105, 103, 107, 106],
    "volume": [1000, 1200, 900, 1100, 1300],
})
hl = find_high_low_timestamps(df_repeated)
check("Correct day high identified (110)", hl["day_high"] == 110)
check("Correct day low identified (95)", hl["day_low"] == 95)
check("Repeated high: first-reached is the EARLIER candle (09:16)",
      hl["high_first_reached_at"].startswith("2026-07-31T09:16"))
check("Repeated high: last-touched is the LATER candle (09:18)",
      hl["high_last_touched_at"].startswith("2026-07-31T09:18"))
check("Repeated low: first-reached is the EARLIER candle (09:15)",
      hl["low_first_reached_at"].startswith("2026-07-31T09:15"))
check("Repeated low: last-touched is the LATER candle (09:19)",
      hl["low_last_touched_at"].startswith("2026-07-31T09:19"))
check("Volume at high candle captured", hl["high_volume"] == 1200)
check("Volume at low candle captured", hl["low_volume"] == 1000)

hl_empty = find_high_low_timestamps(pd.DataFrame())
check("Empty candles: all fields None, no crash", hl_empty["day_high"] is None and hl_empty["day_low"] is None)

# --- Percentage calculations, matching the spec's own worked example ---
print("\n--- Percentage Calculations ---")
mock_kite_calc = MagicMock()
with patch("watchlist_range_analytics.get_instrument_token", return_value=12345), \
     patch("watchlist_range_analytics.determine_session_date", return_value=date(2026, 7, 31)), \
     patch("watchlist_range_analytics.fetch_1min_candles_for_session") as mock_fetch1m, \
     patch("watchlist_range_analytics.get_previous_close", return_value=1445.0):
    calc_times = pd.date_range("2026-07-31 09:15", periods=3, freq="1min", tz=KOLKATA)
    mock_fetch1m.return_value = pd.DataFrame({
        "date": calc_times, "open": [1450.0, 1438.0, 1465.0], "high": [1450.0, 1440.0, 1472.0],
        "low": [1448.0, 1438.0, 1465.0], "close": [1449.0, 1439.0, 1465.0], "volume": [500, 600, 700],
    })
    result = compute_symbol_range_analytics(mock_kite_calc, "RELIANCE", "NSE")

check("Status is COMPLETE with all data present", result["status"] == "COMPLETE")
check("day_open correct (first candle's open)", result["day_open"] == 1450.0)
check("day_high correct", result["day_high"] == 1472.0)
check("day_low correct", result["day_low"] == 1438.0)
check("intraday_range_inr = 1472-1438 = 34.0", result["intraday_range_inr"] == 34.0)
check("low_to_high_pct matches spec example (~2.36%)", abs(result["low_to_high_pct"] - 2.364) < 0.01)
check("open_to_high_pct matches spec example (~1.52%)", abs(result["open_to_high_pct"] - 1.517) < 0.01)
check("previous_close_to_high_pct matches spec example (~1.87%)", abs(result["previous_close_to_high_pct"] - 1.868) < 0.01)
check("close_change_pct matches spec example (~1.38%)", abs(result["close_change_pct"] - 1.384) < 0.01)
check("distance_below_high_pct computed correctly", result["distance_below_high_pct"] is not None)
check("distance_above_low_pct computed correctly", result["distance_above_low_pct"] is not None)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- get_previous_close ---
print("\n--- Previous Close Retrieval ---")
mock_kite_pc = MagicMock()
with patch("watchlist_range_analytics.fetch_candles") as mock_fc:
    daily_dates = pd.to_datetime(["2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]).tz_localize(KOLKATA)
    mock_fc.return_value = pd.DataFrame({
        "date": daily_dates, "open": [100, 101, 102, 103], "high": [105, 106, 107, 108],
        "low": [99, 100, 101, 102], "close": [104, 105, 106.5, 107], "volume": [1000]*4,
    })
    prev_close = get_previous_close(mock_kite_pc, 12345, date(2026, 7, 31))
check("Previous close correctly picks the trading day BEFORE the session date (106.5, from 07-30)",
      prev_close == 106.5)

with patch("watchlist_range_analytics.fetch_candles", return_value=pd.DataFrame()):
    prev_close_empty = get_previous_close(mock_kite_pc, 12345, date(2026, 7, 31))
check("Previous close fails safe to None on empty data (never 0)", prev_close_empty is None)

# --- determine_session_date: weekend fallback ---
print("\n--- Session Date / Weekend Fallback ---")
mock_kite_wk = MagicMock()
with patch("watchlist_range_analytics.fetch_candles") as mock_fc2:
    # Friday 2026-07-31 is the last real trading day before the weekend
    weekday_dates = pd.to_datetime(["2026-07-29", "2026-07-30", "2026-07-31"]).tz_localize(KOLKATA)
    mock_fc2.return_value = pd.DataFrame({
        "date": weekday_dates, "open": [1]*3, "high": [1]*3, "low": [1]*3, "close": [1]*3, "volume": [1]*3,
    })
    session_on_saturday = determine_session_date(mock_kite_wk, 12345, requested_date=date(2026, 8, 1))
check("Weekend requested date falls back to the last real trading day (Friday 07-31)",
      session_on_saturday == date(2026, 7, 31))

# --- No-data fallback ---
with patch("watchlist_range_analytics.fetch_candles", return_value=pd.DataFrame()):
    session_no_data = determine_session_date(mock_kite_wk, 12345, requested_date=date(2026, 8, 1))
check("No daily-candle data at all -> returns None (fail-safe)", session_no_data is None)

# --- Invalid/empty candle data end-to-end ---
print("\n--- Invalid/Empty Candle Handling ---")
with patch("watchlist_range_analytics.get_instrument_token", return_value=12345), \
     patch("watchlist_range_analytics.determine_session_date", return_value=date(2026, 7, 31)), \
     patch("watchlist_range_analytics.fetch_1min_candles_for_session", return_value=pd.DataFrame()):
    result_no_data = compute_symbol_range_analytics(MagicMock(), "EMPTYSYM", "NSE")
check("Empty 1-minute candles -> status NO_DATA", result_no_data["status"] == "NO_DATA")

# Invalid candles: day_high < day_low (nonsensical)
invalid_times = pd.date_range("2026-07-31 09:15", periods=2, freq="1min", tz=KOLKATA)
with patch("watchlist_range_analytics.get_instrument_token", return_value=12345), \
     patch("watchlist_range_analytics.determine_session_date", return_value=date(2026, 7, 31)), \
     patch("watchlist_range_analytics.fetch_1min_candles_for_session") as mock_bad:
    mock_bad.return_value = pd.DataFrame({
        "date": invalid_times, "open": [100, 100], "high": [50, 50],  # high < low, nonsensical
        "low": [100, 100], "close": [100, 100], "volume": [1, 1],
    })
    result_invalid = compute_symbol_range_analytics(MagicMock(), "BADSYM", "NSE")
check("Nonsensical high<low candles -> status INVALID_CANDLES", result_invalid["status"] == "INVALID_CANDLES")

# --- Symbol-level API failure never raises, isolated ---
print("\n--- Per-Symbol Failure Isolation ---")
with patch("watchlist_range_analytics.get_instrument_token", side_effect=Exception("token lookup failed")):
    result_api_err = compute_symbol_range_analytics(MagicMock(), "FAILSYM", "NSE")
check("Instrument token failure returns API_ERROR, does not raise", result_api_err["status"] == "API_ERROR")
check("Error message captured", "token lookup failed" in result_api_err["error"])

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Batch processing: one symbol's failure doesn't stop the rest ---
print("\n--- Batch: One Failure Doesn't Stop the Watchlist ---")
watchlist_3 = [{"symbol": "GOOD1", "exchange": "NSE"}, {"symbol": "BADONE", "exchange": "NSE"},
               {"symbol": "GOOD2", "exchange": "NSE"}]

def fake_compute(kite, symbol, exchange, session_date=None):
    if symbol == "BADONE":
        raise Exception("simulated total failure")
    return {"symbol": symbol, "exchange": exchange, "status": "COMPLETE", "session_date": "2026-07-31"}

with isolated_runtime_paths() as paths:
    with patch("watchlist_range_analytics.compute_symbol_range_analytics", side_effect=fake_compute):
        snapshot = process_watchlist_range_analytics(watchlist_3, MagicMock(), output_path=paths.status_path)

check("Batch processing: all 3 symbols present in the result despite one failure",
      snapshot["processed_count"] == 3)
check("Batch processing: the failing symbol is marked API_ERROR, not silently dropped",
      any(s["symbol"] == "BADONE" and s["status"] == "API_ERROR" for s in snapshot["symbols"]))
check("Batch processing: the other two symbols completed successfully despite the failure",
      snapshot["complete_count"] == 2)

# --- Resume after interruption: already-COMPLETE symbols are not re-fetched ---
print("\n--- Resume / Caching ---")
call_log = []
def tracking_compute(kite, symbol, exchange, session_date=None):
    call_log.append(symbol)
    return {"symbol": symbol, "exchange": exchange, "status": "COMPLETE", "session_date": "2026-07-31"}

with isolated_runtime_paths() as paths:
    # First run: only GOOD1 completes (simulate an interruption after 1 symbol)
    watchlist_2 = [{"symbol": "GOOD1", "exchange": "NSE"}, {"symbol": "GOOD2", "exchange": "NSE"}]
    partial_snapshot = {
        "schema_version": 1, "symbols": [{"symbol": "GOOD1", "exchange": "NSE", "status": "COMPLETE",
                                          "session_date": "2026-07-31"}],
    }
    save_watchlist_snapshot(partial_snapshot, output_path=paths.status_path)

    with patch("watchlist_range_analytics.compute_symbol_range_analytics", side_effect=tracking_compute):
        resumed_snapshot = process_watchlist_range_analytics(watchlist_2, MagicMock(), output_path=paths.status_path)

check("Resume: the already-COMPLETE symbol (GOOD1) was NOT re-fetched", "GOOD1" not in call_log)
check("Resume: only the missing symbol (GOOD2) was actually processed this run", call_log == ["GOOD2"])
check("Resume: final snapshot still contains both symbols", resumed_snapshot["processed_count"] == 2)

# --- Atomic writing + injectable paths ---
print("\n--- Atomic Writing / Injectable Paths ---")
with isolated_runtime_paths() as paths:
    save_watchlist_snapshot({"schema_version": 1, "symbols": []}, output_path=paths.status_path)
    check("Atomic write: file exists after save", os.path.exists(paths.status_path))
    check("Atomic write: no leftover .tmp file after a successful write",
          not os.path.exists(paths.status_path + ".tmp"))
    reloaded = load_watchlist_snapshot(output_path=paths.status_path)
    check("Injectable path: load correctly reads back from the injected path", reloaded["schema_version"] == 1)

# --- JSON serialization with numpy types ---
print("\n--- JSON Serialization ---")
import numpy as np
with isolated_runtime_paths() as paths:
    numpy_data = {"schema_version": 1, "symbols": [
        {"symbol": "NPTEST", "day_high": np.float64(110.5), "high_volume": np.int64(1200)}
    ]}
    try:
        save_watchlist_snapshot(numpy_data, output_path=paths.status_path)
        reloaded_np = load_watchlist_snapshot(output_path=paths.status_path)
        check("numpy-laden data serializes and reloads without crashing",
              reloaded_np["symbols"][0]["day_high"] == 110.5)
        check("Reloaded value is a native Python type, not numpy",
              type(reloaded_np["symbols"][0]["high_volume"]).__name__ == "int")
    except Exception as e:
        check(f"numpy serialization FAILED: {e}", False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
