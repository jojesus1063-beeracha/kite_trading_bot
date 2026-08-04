from datetime import datetime, timedelta
from scheduler import (
    candle_interval_minutes, last_completed_candle_close, next_scan_time, ScanGuard
)

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

print("--- candle_interval_minutes ---")
check("'5minute' parses to 5", candle_interval_minutes("5minute") == 5)
check("'15minute' parses to 15", candle_interval_minutes("15minute") == 15)
check("Unrecognized format defaults to 5", candle_interval_minutes("garbage") == 5)

print("\n--- last_completed_candle_close ---")
# Exact docstring example: 09:37:08 -> last completed close is 09:35:00
now1 = datetime(2026, 7, 31, 9, 37, 8)
result1 = last_completed_candle_close(now1, 5)
check("09:37:08 with 5-min candles -> last completed close is 09:35:00",
      result1 == datetime(2026, 7, 31, 9, 35, 0))

# Exactly on a boundary: 09:35:00 itself -- the 09:30-09:35 candle just completed
now2 = datetime(2026, 7, 31, 9, 35, 0)
result2 = last_completed_candle_close(now2, 5)
check("Exactly on a boundary (09:35:00) -> that boundary itself is the last completed close",
      result2 == datetime(2026, 7, 31, 9, 35, 0))

# One second before a boundary: 09:34:59 -- still the PREVIOUS candle
now3 = datetime(2026, 7, 31, 9, 34, 59)
result3 = last_completed_candle_close(now3, 5)
check("One second before a boundary (09:34:59) -> previous candle (09:30:00) is last completed",
      result3 == datetime(2026, 7, 31, 9, 30, 0))

# 15-min candles
now4 = datetime(2026, 7, 31, 10, 22, 0)
result4 = last_completed_candle_close(now4, 15)
check("10:22:00 with 15-min candles -> last completed close is 10:15:00",
      result4 == datetime(2026, 7, 31, 10, 15, 0))

print("\n--- next_scan_time ---")
# 09:37:08, 5-min candles, 8s buffer -> next close is 09:40:00, plus buffer = 09:40:08
now5 = datetime(2026, 7, 31, 9, 37, 8)
result5 = next_scan_time(now5, 5, 8)
check("09:37:08 -> next scan time is 09:40:08 (next close + 8s buffer)",
      result5 == datetime(2026, 7, 31, 9, 40, 8))

print("\n--- ScanGuard ---")
guard = ScanGuard()
candle_a = datetime(2026, 7, 31, 9, 40, 0)
check("First call for a candle: should_scan is True", guard.should_scan(candle_a) == True)
guard.mark_scanned(candle_a)
check("Same candle again after marking: should_scan is False (prevents duplicate)",
      guard.should_scan(candle_a) == False)

candle_b = datetime(2026, 7, 31, 9, 45, 0)
check("A genuinely NEW candle: should_scan is True", guard.should_scan(candle_b) == True)

# Late-scan-recovery: scheduler wakes up late, current candle has moved
# forward past what was last scanned -- should still scan exactly once
guard2 = ScanGuard()
guard2.mark_scanned(datetime(2026, 7, 31, 9, 40, 0))
much_later_candle = datetime(2026, 7, 31, 9, 55, 0)  # scheduler woke up very late
check("Late-scan-recovery: a much-later candle still triggers should_scan=True",
      guard2.should_scan(much_later_candle) == True)
guard2.mark_scanned(much_later_candle)
check("After recovery-scan, the SAME later candle is not re-scanned",
      guard2.should_scan(much_later_candle) == False)
# And it should never falsely re-trigger for an OLDER candle than what's now marked
older_candle = datetime(2026, 7, 31, 9, 45, 0)
check("An older candle than the last-scanned one is correctly NOT re-scanned",
      guard2.should_scan(older_candle) == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# ============================================================
# Integration test: simulate the REAL run() loop with a fake,
# controlled clock -- runs a genuine ~20 simulated minutes in
# near-instant real time, covering multiple scan cycles and the
# transition into end-of-day square-off.
# ============================================================
print("\n--- Full run() loop simulation (candle-aligned mode) ---")

import datetime as _datetime_module
from unittest.mock import MagicMock
import config as cfg
import main as main_module
from risk_manager import RiskManager

class FakeDateTime(_datetime_module.datetime):
    _current = None
    @classmethod
    def now(cls, tz=None):
        return cls._current

scan_calls = []
position_check_calls = []
exit_orders_placed = []

def fake_run_full_scan(kite, symbols, tokens, exchange_map, open_positions, risk):
    scan_calls.append(FakeDateTime._current)
    return [{"symbol": "TEST", "status": "no signal"}]

def fake_check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
    position_check_calls.append(FakeDateTime._current)
    return "position open"

def fake_sleep(seconds):
    FakeDateTime._current = FakeDateTime._current + _datetime_module.timedelta(seconds=seconds)

def fake_place_exit_order(*args, **kwargs):
    exit_orders_placed.append(FakeDateTime._current)

    quantity = kwargs.get("quantity")
    if quantity is None and len(args) >= 4:
        quantity = args[3]

    quantity = int(quantity or 0)

    return {
        "success": True,
        "order_id": "TEST-EXIT-ORDER",
        "operation_id": None,
        "status": "COMPLETE",
        "reason": None,
        "requested_quantity": quantity,
        "filled_quantity": quantity,
        "average_price": 100.0,
        "exit_confirmation_pending": False,
        "resolved": True,
    }

# Wire everything up
cfg.ENABLE_CANDLE_ALIGNED_POLLING = True
cfg.PAPER_TRADING = True
main_module.datetime = FakeDateTime
main_module.time.sleep = fake_sleep
main_module.get_kite_client = lambda: MagicMock()
main_module.get_instrument_token = lambda kite, s, exch: 12345
main_module.load_positions = lambda: {"PERSIST_TEST": {
    "direction": "BUY", "qty": 5, "entry": 100.0, "stop": 95.0, "target": 110.0,
    "exchange": "NSE", "peak_price": 100.0, "tight_mode": False,
}}
main_module.save_bot_status = lambda *a, **kw: None
main_module.run_full_scan = fake_run_full_scan
main_module.check_position_exit = fake_check_position_exit
main_module.place_exit_order = fake_place_exit_order
main_module.place_force_exit_order = fake_place_exit_order
main_module.record_trade = lambda *a, **kw: None
main_module.save_positions = lambda *a, **kw: None
main_module.clear_positions = lambda: None
main_module.net_pnl_for_trade = lambda *a, **kw: {"gross_pnl": 0.0, "costs": 0.0, "net_pnl": 0.0}

original_watchlist = cfg.WATCHLIST
cfg.WATCHLIST = [{"symbol": "TEST", "exchange": "NSE"}]

# Start the simulated clock at 14:50:00 -- gives us several scan
# cycles before crossing FORCE_SQUARE_OFF_TIME (15:08)
FakeDateTime._current = FakeDateTime(2026, 7, 31, 14, 50, 0)

try:
    main_module.run()
    ran_to_completion = True
except Exception as e:
    print(f"CRASHED: {type(e).__name__} - {e}")
    ran_to_completion = False

cfg.WATCHLIST = original_watchlist
cfg.ENABLE_CANDLE_ALIGNED_POLLING = False

check("Simulation ran to completion without crashing", ran_to_completion)
check("At least 3 full scans occurred over the simulated window", len(scan_calls) >= 3)

# Every scan timestamp should land at a 5-min candle boundary plus the
# configured broker-finalisation safety buffer.
all_aligned = all(
    t.minute % 5 == 0
    and t.second == cfg.SCAN_BUFFER_SECONDS
    for t in scan_calls
)
check(
    "Every scan occurred at a completed candle boundary plus the configured buffer",
    all_aligned,
)

# No duplicate scans for the same candle
scan_minutes = [t.minute for t in scan_calls]
check("No duplicate scans for the same candle", len(scan_minutes) == len(set(scan_minutes)))

check("Position monitoring occurred between scans (multiple checks recorded)",
      len(position_check_calls) > len(scan_calls))

check("End-of-day square-off correctly fired in the 15:08 safety window",
      len(exit_orders_placed) >= 1 and all(
          t.hour == 15 and 8 <= t.minute < 10
          for t in exit_orders_placed
      ))

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# ============================================================
# Targeted test: REAL check_position_exit() (not mocked) fires a
# genuine stop-loss exit promptly, mid-cycle, well before the next
# scheduled full scan -- proves actual responsiveness, not just
# correct timing of the (mocked) position-check calls above.
# ============================================================
print("\n--- Real stop-loss responsiveness within the candle-aligned sub-loop ---")

import pandas as pd
from risk_manager import RiskManager as RiskManagerReal

def make_df_5m(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-31 09:15", periods=n, freq="5min"),
        "open": closes, "high": [c+1 for c in closes], "low": [c-1 for c in closes],
        "close": closes, "volume": [1000]*n,
    })

real_position_check_calls = []
original_check_position_exit = None

def fake_run_full_scan_2(kite, symbols, tokens, exchange_map, open_positions, risk):
    scan_calls.append(FakeDateTime._current)
    return [{"symbol": "TEST", "status": "no signal"}]

# Price starts SAFE, then breaches the stop-loss partway through the
# simulated position-monitoring window -- fetch_candles advances its
# returned price each time it's called, simulating a real market move.
price_sequence = [100.0, 99.0, 97.0, 94.5]  # breaches stop (95.0) on the 4th check
call_counter = {"n": 0}
def fake_fetch_candles(kite, token, interval, lookback_days=1, trim_incomplete=True):
    idx = min(call_counter["n"], len(price_sequence) - 1)
    call_counter["n"] += 1
    return make_df_5m([price_sequence[idx]])

cfg.ENABLE_FIXED_TARGET = True
cfg.ENABLE_CANDLE_ALIGNED_POLLING = True
main_module.fetch_candles = fake_fetch_candles
main_module.run_full_scan = fake_run_full_scan_2
main_module.check_position_exit = main_module.__dict__.get("_real_check_position_exit", None)
# Restore the REAL function (it was overwritten by the mock in the
# simulation above) by re-importing it fresh from the module source.
import importlib
importlib.reload(main_module)
# Re-apply all the mocks reload() just wiped out, EXCEPT check_position_exit
main_module.datetime = FakeDateTime
main_module.time.sleep = fake_sleep
main_module.get_kite_client = lambda: MagicMock()
main_module.get_instrument_token = lambda kite, s, exch: 12345
main_module.load_positions = lambda: {"STOPTEST": {
    "direction": "BUY", "qty": 5, "entry": 100.0, "stop": 95.0, "target": 110.0,
    "exchange": "NSE", "peak_price": 100.0, "tight_mode": False,
}}
main_module.save_bot_status = lambda *a, **kw: None
main_module.run_full_scan = fake_run_full_scan_2
main_module.fetch_candles = fake_fetch_candles
main_module.place_exit_order = fake_place_exit_order
main_module.place_force_exit_order = fake_place_exit_order
main_module.record_trade = lambda *a, **kw: None
main_module.save_positions = lambda *a, **kw: None
main_module.clear_positions = lambda: None

cfg.WATCHLIST = [{"symbol": "TEST", "exchange": "NSE"}]
scan_calls.clear()
exit_orders_placed.clear()
FakeDateTime._current = FakeDateTime(2026, 7, 31, 14, 50, 0)

try:
    main_module.run()
    ran_ok = True
except Exception as e:
    print(f"CRASHED: {type(e).__name__} - {e}")
    ran_ok = False

cfg.WATCHLIST = original_watchlist
cfg.ENABLE_CANDLE_ALIGNED_POLLING = False

check("Real stop-loss test: simulation ran without crashing", ran_ok)
check("Real stop-loss test: a genuine exit order was placed (stop-loss actually fired)",
      len(exit_orders_placed) >= 1)
if exit_orders_placed:
    exit_time = exit_orders_placed[0]
    check("Real stop-loss test: fired BEFORE square-off time (genuine mid-cycle responsiveness, not end-of-day)",
          exit_time.hour == 14 and exit_time.minute < 60 or (exit_time.hour == 15 and exit_time.minute < 8))
    print(f'  (exit fired at {exit_time.strftime("%H:%M:%S")}, square-off is 15:08:00)')

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
