"""
Unit tests for scheduler.py -- pure logic, no live API calls needed.

Run with: python3 -m pytest test_scheduler.py -v
Or standalone: python3 test_scheduler.py
"""

from datetime import datetime

from scheduler import (
    candle_interval_minutes,
    last_completed_candle_close,
    next_scan_time,
    ScanGuard,
    SchedulerHeartbeat,
    SchedulerState,
)


def dt(h, m, s=0):
    return datetime(2026, 7, 25, h, m, s)


# --- candle_interval_minutes ---

def test_interval_parsing_5min():
    assert candle_interval_minutes("5minute") == 5


def test_interval_parsing_3min():
    assert candle_interval_minutes("3minute") == 3

def test_interval_parsing_15min():
    assert candle_interval_minutes("15minute") == 15

def test_interval_parsing_unrecognized_defaults_to_5():
    assert candle_interval_minutes("garbage") == 5


# --- last_completed_candle_close ---

def test_last_completed_mid_candle():
    # 09:37 -> the 09:35 candle is the last COMPLETED one (09:35-09:40 still forming)
    assert last_completed_candle_close(dt(9, 37, 8), 5) == dt(9, 35, 0)

def test_last_completed_exact_boundary():
    # exactly at 09:40:00 -> the 09:35-09:40 candle just completed
    assert last_completed_candle_close(dt(9, 40, 0), 5) == dt(9, 40, 0)

def test_last_completed_one_second_before_boundary():
    # 09:39:59 -> 09:35-09:40 candle NOT yet completed (completes at 09:40:00),
    # so the last COMPLETED candle is 09:30-09:35, whose close is 09:35
    assert last_completed_candle_close(dt(9, 39, 59), 5) == dt(9, 35, 0)

def test_last_completed_at_market_open():
    assert last_completed_candle_close(dt(9, 15, 30), 5) == dt(9, 15, 0)

def test_last_completed_15min_interval():
    # NSE opens 09:15, which is exactly 37*15 min past midnight, so 15-min
    # boundaries land on 09:15/09:30/09:45/10:00/10:15... At 10:02, the most
    # recent boundary passed is 10:00 -- the 09:45-10:00 candle just closed.
    assert last_completed_candle_close(dt(10, 2, 0), 15) == dt(10, 0, 0)


# --- next_scan_time ---

def test_next_scan_basic():
    # now=09:37:08, 5min interval, 8s buffer -> next candle closes 09:40:00, scan at 09:40:08
    assert next_scan_time(dt(9, 37, 8), 5, 8) == dt(9, 40, 8)

def test_next_scan_right_after_boundary():
    # now=09:40:01 (just after a close) -> next scan targets 09:45, not 09:40 again
    assert next_scan_time(dt(9, 40, 1), 5, 8) == dt(9, 45, 8)

def test_next_scan_zero_buffer():
    assert next_scan_time(dt(9, 37, 8), 5, 0) == dt(9, 40, 0)


# --- ScanGuard: duplicate-scan prevention ---

def test_scan_guard_first_scan_always_allowed():
    guard = ScanGuard()
    assert guard.should_scan(dt(9, 35, 0)) is True

def test_scan_guard_blocks_exact_duplicate():
    guard = ScanGuard()
    guard.mark_scanned(dt(9, 35, 0))
    assert guard.should_scan(dt(9, 35, 0)) is False

def test_scan_guard_allows_next_candle():
    guard = ScanGuard()
    guard.mark_scanned(dt(9, 35, 0))
    assert guard.should_scan(dt(9, 40, 0)) is True

def test_scan_guard_blocks_older_candle_after_newer_scanned():
    # simulates a late/out-of-order trigger -- should never re-scan the past
    guard = ScanGuard()
    guard.mark_scanned(dt(9, 40, 0))
    assert guard.should_scan(dt(9, 35, 0)) is False

def test_scan_guard_late_recovery_scans_forward_correctly():
    # scheduler was late; woke up and the "current" completed candle
    # jumped from 09:35 straight to 09:45 (missed 09:40 entirely, e.g.
    # after a long restart) -- should scan the LATEST one, once
    guard = ScanGuard()
    guard.mark_scanned(dt(9, 35, 0))
    assert guard.should_scan(dt(9, 45, 0)) is True
    guard.mark_scanned(dt(9, 45, 0))
    assert guard.should_scan(dt(9, 45, 0)) is False  # now a duplicate


# --- Heartbeat formatting (smoke test -- just confirm it doesn't crash and includes key info) ---

def test_heartbeat_format_includes_key_fields():
    hb = SchedulerHeartbeat(
        current_time=dt(9, 37, 8),
        state=SchedulerState.POSITION_MONITOR,
        last_candle=dt(9, 35, 0),
        next_scan=dt(9, 40, 8),
        watchlist_size=42,
        open_positions_count=2,
        mode="Candle Aligned",
        next_position_check=dt(9, 37, 33),
    )
    output = hb.format()
    assert "09:37:08" in output
    assert "POSITION_MONITOR" in output
    assert "09:35" in output
    assert "09:40:08" in output
    assert "42" in output
    assert "2" in output
    assert "Candle Aligned" in output
    assert "09:37:33" in output

def test_heartbeat_format_handles_none_values():
    # e.g. no open positions yet, no candle seen yet on cold start
    hb = SchedulerHeartbeat(
        current_time=dt(9, 15, 0),
        state=SchedulerState.WAIT_FOR_CANDLE,
        last_candle=None,
        next_scan=None,
        watchlist_size=42,
        open_positions_count=0,
        mode="Candle Aligned",
        next_position_check=None,
    )
    output = hb.format()  # should not raise
    assert "N/A" in output


if __name__ == "__main__":
    import sys
    test_functions = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for test_fn in test_functions:
        try:
            test_fn()
            print(f"PASS: {test_fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {test_fn.__name__} -- {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {test_fn.__name__} -- {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
