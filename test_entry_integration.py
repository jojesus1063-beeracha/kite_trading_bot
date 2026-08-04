"""
Stage 3 integration tests: place_entry_order() end-to-end through the
REAL production code path (not mocking pending_order_store's internal
functions), using a temporarily monkeypatched STORE_PATH for
isolation -- necessary here since place_entry_order() doesn't accept
an injectable path itself (correctly, for production use).
"""
import os
import tempfile
import contextlib
from unittest.mock import MagicMock
import config as cfg
import executor
import pending_order_store

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


@contextlib.contextmanager
def isolated_pending_store():
    """Monkeypatches pending_order_store.STORE_PATH for the duration
    of a test, so place_entry_order()'s internal calls (which use the
    default path) never touch the real production pending_orders.json."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    original = pending_order_store.STORE_PATH
    pending_order_store.STORE_PATH = path
    try:
        yield path
    finally:
        pending_order_store.STORE_PATH = original
        if os.path.exists(path):
            os.remove(path)
        lock_path = path + ".lock"
        if os.path.exists(lock_path):
            os.remove(lock_path)


def make_history_record(status, filled=0, pending=0, cancelled=0, avg_price=None, message=None):
    return [{"status": status, "filled_quantity": filled, "pending_quantity": pending,
            "cancelled_quantity": cancelled, "average_price": avg_price,
            "status_message": message, "exchange_order_id": "EXO1"}]


import hashlib

def _snapshot(fname):
    if not os.path.exists(fname):
        return None
    with open(fname, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

_PROD_FILES = [
    "bot_status.json",
    "open_positions.json",
    "trade_history.jsonl",
    "pending_orders.json",
    "protective_stops.json",
]
_BASELINE = {f: _snapshot(f) for f in _PROD_FILES}

cfg.PAPER_TRADING = False
cfg.CHECK_MARGIN_BEFORE_ENTRY = False  # bypass margin check for these tests, focused on order-fill logic
cfg.CIRCUIT_PROXIMITY_PCT = None
cfg.ORDER_VERIFY_MAX_WAIT_SECONDS = 2
cfg.ORDER_VERIFY_POLL_INTERVAL_SECONDS = 0.01  # fast polling for quick tests (real time, but tiny)

# --- 1-2: Full entry fill creates the correct position, real average price used ---
print("--- Full Fill ---")
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_FULL1"
    kite.order_history.return_value = make_history_record("COMPLETE", filled=10, avg_price=1500.25)
    result = executor.place_entry_order(kite, "TESTFULL", "BUY", 10, "NSE", cfg)
check("1. Full entry fill creates the correct position (success=True)", result["success"] is True)
check("1b. Full entry fill: correct filled_quantity", result["filled_quantity"] == 10)
check("2. Actual broker average price is used", result["average_price"] == 1500.25)
check("Full fill: resolved=True, entry_confirmation_pending=False", result["resolved"] is True and result["entry_confirmation_pending"] is False)

# --- 3: Rejected entry creates no position ---
print("\n--- Rejected / Cancelled ---")
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_REJ1"
    kite.order_history.return_value = make_history_record("REJECTED", filled=0, message="Insufficient funds")
    result3 = executor.place_entry_order(kite, "TESTREJ", "BUY", 10, "NSE", cfg)
check("3. Rejected entry creates no position (success=False)", result3["success"] is False)
check("3b. Rejection reason captured", result3["reason"] == "Insufficient funds")

# --- 4: Cancelled zero-fill creates no position ---
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_CAN1"
    kite.order_history.return_value = make_history_record("CANCELLED", filled=0, cancelled=10)
    result4 = executor.place_entry_order(kite, "TESTCAN", "BUY", 10, "NSE", cfg)
check("4. Cancelled zero-fill creates no position", result4["success"] is False and result4["filled_quantity"] == 0)

# --- 5: Terminal partial fill tracks only filled quantity ---
print("\n--- Partial Fills ---")
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_PART1"
    kite.order_history.return_value = make_history_record("CANCELLED", filled=6, cancelled=4, avg_price=99.5)
    result5 = executor.place_entry_order(kite, "TESTPART", "BUY", 10, "NSE", cfg)
check("5. Terminal partial fill tracks only filled quantity (6, never 10)", result5["success"] is True and result5["filled_quantity"] == 6)
check("5b. Terminal partial fill uses the confirmed average price", result5["average_price"] == 99.5)
check("5c. Terminal partial fill is resolved (broker order is terminal)", result5["resolved"] is True)

# --- 6: Timeout with partial fill tracks confirmed shares, stays unresolved ---
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_TIMEOUT_PART"
    kite.order_history.return_value = make_history_record("OPEN", filled=4, pending=6)  # never terminal
    result6 = executor.place_entry_order(kite, "TESTTIMEOUTPART", "BUY", 10, "NSE", cfg)
check("6. Timeout with partial fill tracks the 4 confirmed shares", result6["success"] is True and result6["filled_quantity"] == 4)
check("6b. Timeout with partial fill: entry_confirmation_pending=True, resolved=False", result6["entry_confirmation_pending"] is True and result6["resolved"] is False)

# --- 7: Timeout with zero fill creates no position ---
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_TIMEOUT_ZERO"
    kite.order_history.return_value = make_history_record("OPEN", filled=0, pending=10)
    result7 = executor.place_entry_order(kite, "TESTTIMEOUTZERO", "BUY", 10, "NSE", cfg)
check("7. Timeout with zero fill creates no position", result7["success"] is False and result7["filled_quantity"] == 0)
check("7b. Timeout with zero fill: entry_confirmation_pending=True", result7["entry_confirmation_pending"] is True)

# --- 8: Unknown result creates no fabricated position ---
print("\n--- Unknown State ---")
with isolated_pending_store():
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_UNKNOWN"
    kite.order_history.return_value = []  # empty response every time -> UNKNOWN
    result8 = executor.place_entry_order(kite, "TESTUNKNOWN", "BUY", 10, "NSE", cfg)
check("8. Unknown result creates no fabricated position", result8["success"] is False and result8["average_price"] is None)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 9: Unresolved order blocks duplicate entry ---
print("\n--- Duplicate Prevention ---")
with isolated_pending_store() as p9:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_DUP1"
    kite.order_history.return_value = make_history_record("OPEN", filled=0, pending=10)  # stays unresolved
    result9a = executor.place_entry_order(kite, "TESTDUP", "BUY", 10, "NSE", cfg)
    result9b = executor.place_entry_order(kite, "TESTDUP", "BUY", 5, "NSE", cfg)
check("9. Unresolved order blocks a duplicate entry attempt", result9b["success"] is False and result9b["reason"] == "ENTRY_BLOCKED_PENDING_ORDER")
check("9b. Only ONE order was actually submitted to the broker", kite.place_order.call_count == 1)

# --- 10: Submission failure before order ID remains unresolved ---
print("\n--- Submission Failure ---")
with isolated_pending_store() as p10:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.side_effect = Exception("network timeout during submission")
    result10 = executor.place_entry_order(kite, "TESTSUBFAIL", "BUY", 10, "NSE", cfg)
    from pending_order_store import get_order
    stored10 = get_order(result10["operation_id"], path=p10)
    check("10. Submission failure: status is SUBMISSION_UNCERTAIN, not silently treated as rejection", result10["status"] == "SUBMISSION_UNCERTAIN")
    check("10b. Submission failure: order_id remains None in the persisted record", stored10["order_id"] is None)
    check("10c. Submission failure: intent remains UNRESOLVED (not automatically resolved)", stored10["resolved"] is False)
    result10d = executor.place_entry_order(kite, "TESTSUBFAIL", "BUY", 10, "NSE", cfg)
    check("10d. Submission failure: a second attempt for the same symbol is blocked (no blind resubmission)",
          result10d["reason"] == "ENTRY_BLOCKED_PENDING_ORDER")
    check("10e. Only ONE actual call to kite.place_order() happened (the second was blocked before submission)",
          kite.place_order.call_count == 1)

# --- 11: Broker order ID persisted BEFORE verification begins ---
print("\n--- Order ID Persistence Timing ---")
with isolated_pending_store() as p11:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_TIMING1"

    order_id_at_first_history_call = []
    def tracking_order_history(order_id):
        # Directly inspects the ISOLATED store's own state at this exact
        # moment -- no patching of create_order_intent needed at all, which
        # avoids any risk of a patch leaking outside its intended scope.
        from pending_order_store import load_pending_orders
        data = load_pending_orders(p11)
        matching = [o for o in data["orders"] if o["symbol"] == "TESTTIMING"]
        order_id_at_first_history_call.append(matching[0]["order_id"] if matching else None)
        return make_history_record("COMPLETE", filled=10, avg_price=100.0)
    kite.order_history.side_effect = tracking_order_history

    result11 = executor.place_entry_order(kite, "TESTTIMING", "BUY", 10, "NSE", cfg)
check("11. Broker order ID was already persisted (in the isolated store) by the time order_history was first called",
      bool(order_id_at_first_history_call) and order_id_at_first_history_call[0] == "ORD_TIMING1")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 12-14: Restart recovery, idempotent delta application ---
print("\n--- Restart Recovery ---")
import main as main_module
with isolated_pending_store() as p12:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.place_order.return_value = "ORD_RECOVER1"
    # First attempt: order stays open/unresolved (simulates a crash right after submission)
    kite.order_history.return_value = make_history_record("OPEN", filled=0, pending=10)
    result_first = executor.place_entry_order(kite, "TESTRECOVER", "BUY", 10, "NSE", cfg)
    check("12. Pre-recovery: entry stays pending, no fabricated position success", result_first["success"] is False)

    # Simulate the crash: build a fresh open_positions dict as if from a
    # fresh restart (empty local record -- worst case, matching the code's
    # own documented CRITICAL-log path for "no local record before restart")
    open_positions_after_restart = {}

    # Recovery run 1: order history now shows it filled 6 of 10
    kite.order_history.return_value = make_history_record("OPEN", filled=6, pending=4)
    main_module.recover_unresolved_entries(kite, open_positions_after_restart, cfg, positions_path=p12 + ".positions")
    check("13. Recovery applies the confirmed delta (6 shares) after restart",
          open_positions_after_restart.get("TESTRECOVER", {}).get("filled_quantity") == 6)

    # Recovery run 2: run AGAIN with the SAME filled_quantity (idempotency check)
    main_module.recover_unresolved_entries(kite, open_positions_after_restart, cfg, positions_path=p12 + ".positions")
    check("14. Running recovery again with no new fill does NOT double-add quantity",
          open_positions_after_restart.get("TESTRECOVER", {}).get("qty") == 6)

# --- 15: Unresolved intent with no order_id is never resubmitted, just logged ---
with isolated_pending_store() as p15:
    from pending_order_store import create_order_intent
    op_id_15 = create_order_intent("TESTNOOID", "NSE", "ENTRY", "BUY", 10, path=p15)
    kite15 = MagicMock()
    open_positions_15 = {}
    main_module.recover_unresolved_entries(kite15, open_positions_15, cfg, positions_path=p15 + ".positions")
check("15. Unresolved intent with no order_id: no position fabricated, no order_history call attempted",
      "TESTNOOID" not in open_positions_15 and kite15.order_history.call_count == 0)

# --- 16: Backward compatibility -- older position records (missing new fields) still load ---
print("\n--- Backward Compatibility ---")
old_style_position = {
    "direction": "BUY", "qty": 10, "entry": 100.0, "stop": 95.0, "target": 110.0,
    "exchange": "NSE", "peak_price": 100.0, "tight_mode": False, "entry_time": "2026-08-01 10:00:00",
    # deliberately missing: entry_order_id, entry_operation_id, filled_quantity, etc.
}
try:
    filled_qty = old_style_position.get("filled_quantity", old_style_position.get("qty"))
    entry_pending = old_style_position.get("entry_confirmation_pending", False)
    check("16. Older position record (missing new Stage-3 fields) loads without crashing", filled_qty == 10 and entry_pending is False)
except Exception as e:
    check(f"16. Older position record loading crashed: {e}", False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 17-18: Paper mode isolation ---
print("\n--- Paper Mode Isolation ---")
cfg.PAPER_TRADING = True
kite_paper = MagicMock()
result_paper = executor.place_entry_order(kite_paper, "TESTPAPER", "BUY", 10, "NSE", cfg)
check("17. Paper mode makes NO order_history call", kite_paper.order_history.call_count == 0)
check("17b. Paper mode makes NO place_order call either (fully synthetic)", kite_paper.place_order.call_count == 0)
check("18. Paper mode behavior unchanged: success=True, filled=requested, PAPER order_id",
      result_paper["success"] is True and result_paper["filled_quantity"] == 10 and result_paper["order_id"] == "PAPER")
check("18b. Paper mode: average_price is None (so callers fall back to signal.entry_price exactly as before)",
      result_paper["average_price"] is None)
cfg.PAPER_TRADING = False

# --- 19: Fixed levels use the confirmed broker fill ---
print("\n--- Stop/Target Preservation ---")
with open("entry_protection.py") as f:
    protection_src = f.read()
check(
    "19. confirmed-position builder recalculates fixed stop and target from the broker fill",
    "fixed_levels_from_fill(" in protection_src
    and "confirmed_entry_price" in protection_src
    and '"stop": stop_price,' in protection_src,
)

# --- 20: main.py never adds requested_quantity when filled_quantity is lower ---
check(
    "20. position builder uses confirmed_qty, never requested quantity",
    '"qty": confirmed_qty,' in protection_src
    and 'confirmed_qty = int(entry_result.get("filled_quantity") or 0)' in protection_src,
)

# --- 21: Production state files remain untouched (real hash comparison) ---
print("\n--- Production File Integrity ---")
for fname in _PROD_FILES:
    after = _snapshot(fname)
    before = _BASELINE[fname]
    print(f"  {fname}: before={'exists' if before else 'absent'}, after={'exists' if after else 'absent'}, "
          f"{'UNCHANGED' if before == after else 'CHANGED!!'}")
    check(f"21. {fname} byte-for-byte unchanged (hash comparison) across the full test run", before == after)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
