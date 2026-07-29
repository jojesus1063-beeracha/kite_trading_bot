import os
import shutil
from unittest.mock import patch
import signal_log

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

TEST_DIR = "/tmp/test_signal_logs"
if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
signal_log.SIGNAL_LOG_DIR = TEST_DIR

with patch("signal_log.date") as mock_date:
    mock_date.today.return_value.isoformat.return_value = "2026-07-29"
    r1 = signal_log.log_signal({"symbol": "SBIN", "executed": True})
    check("First log_signal call returns True", r1 is True)
    r2 = signal_log.log_signal({"symbol": "TCS", "executed": False})
    check("Second log_signal call returns True", r2 is True)
    records = signal_log.load_signals_for_date("2026-07-29")
    check("Both records appended, none overwritten", len(records) == 2)
    check("First record symbol correct", records[0]["symbol"] == "SBIN")
    check("Executed flags preserved correctly", records[0]["executed"] is True and records[1]["executed"] is False)

with patch("signal_log.date") as mock_date:
    mock_date.today.return_value.isoformat.return_value = "2026-07-30"
    signal_log.log_signal({"symbol": "INFY", "executed": True})
    day1 = signal_log.load_signals_for_date("2026-07-29")
    day2 = signal_log.load_signals_for_date("2026-07-30")
    check("New day creates a SEPARATE file", len(day1) == 2 and len(day2) == 1)

check("Loading a date with no file returns empty list", signal_log.load_signals_for_date("2099-01-01") == [])

original_open = open
def failing_open(*args, **kwargs):
    if TEST_DIR in str(args[0]):
        raise IOError("simulated disk failure")
    return original_open(*args, **kwargs)

with patch("builtins.open", failing_open):
    result = signal_log.log_signal({"symbol": "FAILTEST"})
    check("Write failure returns False, does not raise", result is False)

shutil.rmtree(TEST_DIR)
print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
