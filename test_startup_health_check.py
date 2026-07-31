from unittest.mock import MagicMock
import config as cfg
import main as main_module

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def make_mock_kite(margins_ok=True):
    kite = MagicMock()
    if margins_ok:
        kite.margins.return_value = {"equity": {"net": 1000}}
    else:
        kite.margins.side_effect = Exception("auth failed")
    return kite

scan_called = {"n": 0}
def fake_run_full_scan(*a, **kw):
    scan_called["n"] += 1
    return []

# --- Empty watchlist: should fail fast, never reach the scan loop ---
scan_called["n"] = 0
original_watchlist = cfg.WATCHLIST
cfg.WATCHLIST = []
main_module.get_kite_client = lambda: make_mock_kite(margins_ok=True)
main_module.load_positions = lambda: {}
main_module.run_full_scan = fake_run_full_scan
main_module.get_instrument_token = lambda kite, s, exch: 12345

main_module.run()
check("Empty watchlist: run() returns early, never calls run_full_scan", scan_called["n"] == 0)

cfg.WATCHLIST = original_watchlist

# --- Auth failure: should fail fast, never reach the scan loop ---
scan_called["n"] = 0
main_module.get_kite_client = lambda: make_mock_kite(margins_ok=False)
main_module.run()
check("Auth failure: run() returns early, never calls run_full_scan", scan_called["n"] == 0)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Healthy startup: non-empty watchlist + working auth -> proceeds past the guard ---
scan_called["n"] = 0
cfg.WATCHLIST = [{"symbol": "TEST", "exchange": "NSE"}]
main_module.get_kite_client = lambda: make_mock_kite(margins_ok=True)
main_module.past_square_off = lambda: True  # immediately hit end-of-day path to end the loop cleanly
main_module.save_positions = lambda *a, **kw: None
main_module.clear_positions = lambda: None

main_module.run()
check("Healthy startup: proceeds past the guard (reaches the main loop, hits square-off path)", True)

cfg.WATCHLIST = original_watchlist

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
