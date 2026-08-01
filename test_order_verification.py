import time as real_time
from unittest.mock import MagicMock
from order_verification import verify_order_execution, OrderExecutionResult

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeClock:
    """Deterministic, injectable clock -- advances only when sleep_fn is called."""
    def __init__(self):
        self.now = 0.0
    def tick(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def make_record(status, filled=0, pending=0, cancelled=0, avg_price=None, message=None, exchange_order_id="EXO123"):
    return {"status": status, "filled_quantity": filled, "pending_quantity": pending,
            "cancelled_quantity": cancelled, "average_price": avg_price,
            "status_message": message, "exchange_order_id": exchange_order_id}


# --- 1: Immediate complete fill ---
print("--- Terminal States ---")
clock = FakeClock()
kite = MagicMock()
kite.order_history.return_value = [make_record("COMPLETE", filled=10, pending=0, avg_price=105.5)]
result = verify_order_execution(kite, "ORD1", 10, clock_fn=clock.tick, sleep_fn=clock.sleep)
check("Immediate complete fill: status COMPLETE", result.status == "COMPLETE")
check("Immediate complete fill: correct filled_quantity", result.filled_quantity == 10)
check("Immediate complete fill: correct average_price", result.average_price == 105.5)
check("Immediate complete fill: terminal=True", result.terminal is True)

# --- 2: Pending followed by complete ---
clock2 = FakeClock()
kite2 = MagicMock()
kite2.order_history.side_effect = [
    [make_record("OPEN", pending=10)],
    [make_record("COMPLETE", filled=10, avg_price=200.0)],
]
result2 = verify_order_execution(kite2, "ORD2", 10, poll_interval_seconds=1, clock_fn=clock2.tick, sleep_fn=clock2.sleep)
check("Pending then complete: eventually resolves to COMPLETE", result2.status == "COMPLETE")
check("Pending then complete: correct history_attempts (2 calls)", result2.history_attempts == 2)

# --- 3: Rejected order ---
kite3 = MagicMock()
kite3.order_history.return_value = [make_record("REJECTED", message="Insufficient margin")]
_clk1 = FakeClock()
result3 = verify_order_execution(kite3, "ORD3", 10, clock_fn=_clk1.tick, sleep_fn=_clk1.sleep)
check("Rejected order: status REJECTED", result3.status == "REJECTED")
check("Rejected order: zero filled quantity", result3.filled_quantity == 0)
check("Rejected order: status_message preserved", result3.status_message == "Insufficient margin")
check("Rejected order: average_price is None (never fabricated)", result3.average_price is None)
check("Rejected order: terminal=True", result3.terminal is True)

# --- 4: Cancelled with zero fill ---
kite4 = MagicMock()
kite4.order_history.return_value = [make_record("CANCELLED", filled=0, cancelled=10)]
_clk2 = FakeClock()
result4 = verify_order_execution(kite4, "ORD4", 10, clock_fn=_clk2.tick, sleep_fn=_clk2.sleep)
check("Cancelled zero fill: status CANCELLED (not PARTIALLY_FILLED)", result4.status == "CANCELLED")
check("Cancelled zero fill: average_price is None", result4.average_price is None)

# --- 5: Cancelled after partial fill ---
kite5 = MagicMock()
kite5.order_history.return_value = [make_record("CANCELLED", filled=6, cancelled=4, avg_price=99.0)]
_clk3 = FakeClock()
result5 = verify_order_execution(kite5, "ORD5", 10, clock_fn=_clk3.tick, sleep_fn=_clk3.sleep)
check("Cancelled after partial fill: status PARTIALLY_FILLED (not CANCELLED, not COMPLETE)", result5.status == "PARTIALLY_FILLED")
check("Cancelled after partial fill: correct filled_quantity (6)", result5.filled_quantity == 6)
check("Cancelled after partial fill: correct cancelled_quantity (4)", result5.cancelled_quantity == 4)
check("Cancelled after partial fill: real average_price preserved", result5.average_price == 99.0)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 6: Partial fill still open at timeout ---
print("\n--- Timeout / Unknown States ---")
clock6 = FakeClock()
kite6 = MagicMock()
kite6.order_history.return_value = [make_record("OPEN", filled=6, pending=4)]  # never becomes terminal
result6 = verify_order_execution(kite6, "ORD6", 10, max_wait_seconds=5, poll_interval_seconds=1,
                                  clock_fn=clock6.tick, sleep_fn=clock6.sleep)
check("Partial fill stuck open: status TIMEOUT (not a terminal broker state)", result6.status == "TIMEOUT")
check("Partial fill stuck open: filled_quantity still reported accurately (6)", result6.filled_quantity == 6)
check("Partial fill stuck open: terminal=False", result6.terminal is False)

# --- 7: Empty history response ---
kite7 = MagicMock()
kite7.order_history.return_value = []
_clk4 = FakeClock()
result7 = verify_order_execution(kite7, "ORD7", 10, max_wait_seconds=3, poll_interval_seconds=1,
                                  clock_fn=_clk4.tick, sleep_fn=_clk4.sleep)
check("Empty history response: status UNKNOWN (never treated as success)", result7.status == "UNKNOWN")
check("Empty history response: average_price is None", result7.average_price is None)

# --- 8: Malformed history record ---
kite8 = MagicMock()
kite8.order_history.return_value = [{"status": "COMPLETE", "filled_quantity": "not-a-number"}]
_clk5 = FakeClock()
result8 = verify_order_execution(kite8, "ORD8", 10, max_wait_seconds=3, poll_interval_seconds=1,
                                  clock_fn=_clk5.tick, sleep_fn=_clk5.sleep)
check("Malformed record (bad type): does not crash, resolves to UNKNOWN", result8.status == "UNKNOWN")

# --- 9: Transient API exception followed by completion ---
kite9 = MagicMock()
kite9.order_history.side_effect = [Exception("network blip"), [make_record("COMPLETE", filled=10, avg_price=50.0)]]
_clk6 = FakeClock()
result9 = verify_order_execution(kite9, "ORD9", 10, poll_interval_seconds=1, clock_fn=_clk6.tick, sleep_fn=_clk6.sleep)
check("Transient exception then success: resolves to COMPLETE", result9.status == "COMPLETE")
check("Transient exception then success: api_error_count reflects the one failure", result9.api_error_count == 1)

# --- 10: Repeated API exceptions until timeout ---
clock10 = FakeClock()
kite10 = MagicMock()
kite10.order_history.side_effect = Exception("persistent outage")
result10 = verify_order_execution(kite10, "ORD10", 10, max_wait_seconds=5, poll_interval_seconds=1,
                                   clock_fn=clock10.tick, sleep_fn=clock10.sleep)
check("Repeated exceptions until timeout: resolves to UNKNOWN (never obtained a valid record)", result10.status == "UNKNOWN")
check("Repeated exceptions until timeout: api_error_count reflects every failed attempt", result10.api_error_count == result10.history_attempts)

# --- 11: Average price absent when filled quantity is zero (raw data HAS a price, must still be suppressed) ---
print("\n--- Price Fabrication Prevention ---")
kite11 = MagicMock()
kite11.order_history.return_value = [make_record("REJECTED", filled=0, avg_price=999.0)]  # nonsensical but real API could send this
_clk7 = FakeClock()
result11 = verify_order_execution(kite11, "ORD11", 10, clock_fn=_clk7.tick, sleep_fn=_clk7.sleep)
check("Raw avg_price present but filled=0: result.average_price is still None (never fabricated)", result11.average_price is None)

# --- 12: Actual broker average price preserved ---
kite12 = MagicMock()
kite12.order_history.return_value = [make_record("COMPLETE", filled=10, avg_price=1234.56)]
_clk8 = FakeClock()
result12 = verify_order_execution(kite12, "ORD12", 10, clock_fn=_clk8.tick, sleep_fn=_clk8.sleep)
check("Real fill: exact average_price preserved (1234.56)", result12.average_price == 1234.56)

# --- 13: Latest order-history state selected correctly (multiple records in one response) ---
print("\n--- Latest State Selection ---")
kite13 = MagicMock()
kite13.order_history.return_value = [
    make_record("OPEN", pending=10),
    make_record("COMPLETE", filled=10, avg_price=77.0),  # the LATEST record -- must be the one used
]
_clk9 = FakeClock()
result13 = verify_order_execution(kite13, "ORD13", 10, clock_fn=_clk9.tick, sleep_fn=_clk9.sleep)
check("Multiple records in one response: uses the LAST (most recent) record, not the first", result13.status == "COMPLETE" and result13.average_price == 77.0)

# --- 14: No real sleeping in tests ---
print("\n--- No Real Sleeping ---")
real_start = real_time.time()
clock14 = FakeClock()
kite14 = MagicMock()
kite14.order_history.return_value = [make_record("OPEN", pending=10)]  # never resolves
verify_order_execution(kite14, "ORD14", 10, max_wait_seconds=15, poll_interval_seconds=1,
                       clock_fn=clock14.tick, sleep_fn=clock14.sleep)
real_elapsed = real_time.time() - real_start
check(f"A 15-second simulated timeout took {real_elapsed:.3f}s of REAL wall-clock time (should be near-instant)",
      real_elapsed < 1.0)

# --- 15: Existing paper/live paths remain untouched (Stage 1 makes zero live-path changes) ---
print("\n--- No Live-Path Integration Yet ---")
with open("main.py") as f:
    main_content = f.read()
with open("executor.py") as f:
    executor_content = f.read()
check("main.py does not yet import order_verification (Stage 1 is standalone)",
      "order_verification" not in main_content)
check("executor.py does not yet import order_verification (Stage 1 is standalone)",
      "order_verification" not in executor_content)
check("executor.py's place_exit_order still returns the original SUBMITTED status (unchanged)",
      '"status": "SUBMITTED"' in executor_content)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
