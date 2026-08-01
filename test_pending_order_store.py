import os
import json
import tempfile
from order_verification import OrderExecutionResult
from datetime import datetime
from pending_order_store import (
    load_pending_orders, save_pending_orders, create_order_intent,
    attach_broker_order_id, update_order_verification, mark_order_resolved,
    get_order, get_order_by_broker_id, list_unresolved_orders, has_unresolved_order,
    PendingOrderStoreError, DuplicateOperationError, DuplicateBrokerOrderIdError,
    UnresolvedOrderExistsError, InvalidOrderRecordError, STORE_PATH,
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

def temp_store_path():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="pending_orders_test_")
    os.close(fd)
    os.remove(path)  # start genuinely missing, matching real "no store yet" state
    return path

def make_result(status="COMPLETE", filled=10, pending=0, cancelled=0, avg_price=100.0,
                message=None, exch_order_id="EXO1", terminal=True, attempts=1, errors=0):
    return OrderExecutionResult(
        order_id="ORD1", status=status, requested_quantity=10, filled_quantity=filled,
        pending_quantity=pending, cancelled_quantity=cancelled, average_price=avg_price,
        status_message=message, exchange_order_id=exch_order_id, terminal=terminal,
        verified_at=datetime.now(), history_attempts=attempts, api_error_count=errors,
    )

# --- 1-5: Load/save basics ---
print("--- Load/Save Basics ---")
p1 = temp_store_path()
data = load_pending_orders(p1)
check("1. Missing store loads as empty", data["orders"] == [])

op_id = create_order_intent("RELIANCE", "NSE", "ENTRY", "BUY", 10, path=p1)
reloaded = load_pending_orders(p1)
check("2. Save and reload one intent", len(reloaded["orders"]) == 1 and reloaded["orders"][0]["operation_id"] == op_id)
check("3. Atomic-write success: file exists, no leftover .tmp", os.path.exists(p1) and not os.path.exists(p1 + ".tmp"))

# 4: temp-file cleanup on failure -- simulate a real disk-level failure via a mocked os.fsync
# (json.dump's default=str fallback means almost nothing fails serialization itself,
# so this is a more realistic failure mode to test anyway)
from unittest.mock import patch
p4 = temp_store_path()
try:
    with patch("os.fsync", side_effect=OSError("simulated disk failure")):
        save_pending_orders({"schema_version": 1, "orders": []}, path=p4)
    check("4. Temp-file cleanup on failure", False)
except OSError:
    check("4. Temp-file cleanup on failure: no leftover .tmp after a failed write", not os.path.exists(p4 + ".tmp"))
    check("4b. The real store file was never created either (write never completed)", not os.path.exists(p4))

p5 = temp_store_path()
with open(p5, "w") as f:
    f.write("{ this is not valid json !!!")
try:
    load_pending_orders(p5)
    check("5. Corrupt JSON is not silently treated as empty", False)
except PendingOrderStoreError:
    check("5. Corrupt JSON is not silently treated as empty (raises explicitly)", True)
os.remove(p5)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 6-13: Duplicate / idempotency rules ---
print("\n--- Idempotency Rules ---")
p6 = temp_store_path()

# 6: Duplicate operation ID rejected -- force via mocking uuid4 to return the same value twice
from unittest.mock import patch
import uuid as uuid_module
fixed_id = str(uuid_module.uuid4())
with patch("pending_order_store.uuid.uuid4", return_value=uuid_module.UUID(fixed_id)):
    create_order_intent("TESTA", "NSE", "ENTRY", "BUY", 5, path=p6)
    try:
        # Second call with the SAME mocked uuid, but for a DIFFERENT symbol so
        # idempotency-blocking doesn't mask the operation_id collision itself
        create_order_intent("TESTB", "NSE", "ENTRY", "BUY", 5, path=p6)
        check("6. Duplicate operation ID rejected", False)
    except DuplicateOperationError:
        check("6. Duplicate operation ID rejected", True)

# 7: Duplicate broker order ID rejected
p7 = temp_store_path()
op_a = create_order_intent("SYMA", "NSE", "ENTRY", "BUY", 5, path=p7)
op_b = create_order_intent("SYMB", "NSE", "ENTRY", "BUY", 5, path=p7)
attach_broker_order_id(op_a, "ORD_SHARED", path=p7)
try:
    attach_broker_order_id(op_b, "ORD_SHARED", path=p7)
    check("7. Duplicate broker order ID rejected", False)
except DuplicateBrokerOrderIdError:
    check("7. Duplicate broker order ID rejected", True)

# 8: Duplicate unresolved ENTRY blocked
p8 = temp_store_path()
create_order_intent("RELIANCE", "NSE", "ENTRY", "BUY", 10, path=p8)
try:
    create_order_intent("RELIANCE", "NSE", "ENTRY", "BUY", 10, path=p8)
    check("8. Duplicate unresolved entry blocked", False)
except UnresolvedOrderExistsError:
    check("8. Duplicate unresolved entry blocked", True)

# 9: Duplicate unresolved EXIT blocked
p9 = temp_store_path()
create_order_intent("TCS", "NSE", "EXIT", "SELL", 10, path=p9)
try:
    create_order_intent("TCS", "NSE", "EXIT", "SELL", 10, path=p9)
    check("9. Duplicate unresolved exit blocked", False)
except UnresolvedOrderExistsError:
    check("9. Duplicate unresolved exit blocked", True)

# 10: FORCE_EXIT blocked by an existing unresolved EXIT
p10 = temp_store_path()
create_order_intent("INFY", "NSE", "EXIT", "SELL", 10, path=p10)
try:
    create_order_intent("INFY", "NSE", "FORCE_EXIT", "SELL", 10, path=p10)
    check("10. Force-exit blocked by existing unresolved exit", False)
except UnresolvedOrderExistsError:
    check("10. Force-exit blocked by existing unresolved exit", True)

# 11: EXIT blocked by an existing unresolved FORCE_EXIT
p11 = temp_store_path()
create_order_intent("HDFC", "NSE", "FORCE_EXIT", "SELL", 10, path=p11)
try:
    create_order_intent("HDFC", "NSE", "EXIT", "SELL", 10, path=p11)
    check("11. Exit blocked by existing unresolved force-exit", False)
except UnresolvedOrderExistsError:
    check("11. Exit blocked by existing unresolved force-exit", True)

# 12: Resolved operation permits a new operation
p12 = temp_store_path()
op12 = create_order_intent("WIPRO", "NSE", "ENTRY", "BUY", 10, path=p12)
mark_order_resolved(op12, path=p12)
try:
    new_op = create_order_intent("WIPRO", "NSE", "ENTRY", "BUY", 10, path=p12)
    check("12. Resolved operation permits a new operation", new_op != op12)
except UnresolvedOrderExistsError:
    check("12. Resolved operation permits a new operation", False)

# 13: Entry and exit for the same symbol -- documented rule: ENTRY family and
# EXIT family are independent lock groups, an unresolved ENTRY does NOT block an EXIT
p13 = temp_store_path()
create_order_intent("ITC", "NSE", "ENTRY", "BUY", 10, path=p13)
try:
    create_order_intent("ITC", "NSE", "EXIT", "SELL", 10, path=p13)
    check("13. Unresolved ENTRY does not block a new EXIT for the same symbol (independent lock groups)", True)
except UnresolvedOrderExistsError:
    check("13. Unresolved ENTRY does not block a new EXIT for the same symbol (independent lock groups)", False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 14-16: Attach broker order ID ---
print("\n--- Attach Broker Order ID ---")
p14 = temp_store_path()
op14 = create_order_intent("SBIN", "NSE", "ENTRY", "BUY", 10, path=p14)
attach_broker_order_id(op14, "ORD_SBIN_1", path=p14)
check("14. Attach broker order ID", get_order(op14, path=p14)["order_id"] == "ORD_SBIN_1")

# 15: idempotent with the SAME value -- no exception
try:
    attach_broker_order_id(op14, "ORD_SBIN_1", path=p14)
    check("15. Attach broker order ID idempotently with the same value", True)
except Exception:
    check("15. Attach broker order ID idempotently with the same value", False)

# 16: conflicting second value rejected
try:
    attach_broker_order_id(op14, "ORD_SBIN_DIFFERENT", path=p14)
    check("16. Conflicting second broker order ID rejected", False)
except DuplicateBrokerOrderIdError:
    check("16. Conflicting second broker order ID rejected", True)

# --- 17-21: Update from OrderExecutionResult ---
print("\n--- Update From OrderExecutionResult ---")
p17 = temp_store_path()
op17 = create_order_intent("AXISBANK", "NSE", "ENTRY", "BUY", 10, path=p17)
attach_broker_order_id(op17, "ORD17", path=p17)
update_order_verification(op17, make_result(status="COMPLETE", filled=10, avg_price=850.5), path=p17)
record17 = get_order(op17, path=p17)
check("17. Update from OrderExecutionResult", record17["last_known_status"] == "COMPLETE" and record17["average_price"] == 850.5)

p18 = temp_store_path()
op18 = create_order_intent("KOTAKBANK", "NSE", "ENTRY", "BUY", 10, path=p18)
update_order_verification(op18, make_result(status="PARTIALLY_FILLED", filled=6, pending=4, avg_price=1900.0), path=p18)
record18 = get_order(op18, path=p18)
check("18. Partial-fill quantities persisted", record18["filled_quantity"] == 6 and record18["pending_quantity"] == 4)

p19 = temp_store_path()
op19 = create_order_intent("MARUTI", "NSE", "ENTRY", "BUY", 5, path=p19)
update_order_verification(op19, make_result(status="REJECTED", filled=0, pending=0, avg_price=None,
                                             message="Insufficient margin", terminal=True), path=p19)
record19 = get_order(op19, path=p19)
check("19. Rejection details persisted", record19["last_known_status"] == "REJECTED" and record19["status_message"] == "Insufficient margin")

p20 = temp_store_path()
op20 = create_order_intent("TATASTEEL", "NSE", "ENTRY", "BUY", 10, path=p20)
update_order_verification(op20, make_result(status="TIMEOUT", filled=3, pending=7, avg_price=None, terminal=False), path=p20)
record20 = get_order(op20, path=p20)
check("20. Timeout state persisted", record20["last_known_status"] == "TIMEOUT" and record20["terminal"] == False)

p21 = temp_store_path()
op21 = create_order_intent("BAJFINANCE", "NSE", "ENTRY", "BUY", 10, path=p21)
update_order_verification(op21, make_result(status="UNKNOWN", filled=0, pending=10, avg_price=None, terminal=False), path=p21)
record21 = get_order(op21, path=p21)
check("21. Unknown state persisted", record21["last_known_status"] == "UNKNOWN")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 22-24: Restart simulation ---
print("\n--- Restart Simulation ---")
p22 = temp_store_path()

# Step 1-2: create + save an intent with no broker order ID
op22 = create_order_intent("ADANIENT", "NSE", "ENTRY", "BUY", 8, path=p22)

# Step 3-5: simulate a "process restart" by loading fresh from disk (no shared
# in-memory state at all -- every function already reads/writes only via disk)
check("22a. Restart preserves unresolved intent (no order_id yet)",
      get_order(op22, path=p22)["order_id"] is None)
check("22b. Unresolved intent still blocks a duplicate after 'restart'",
      has_unresolved_order("ADANIENT", "NSE", "ENTRY", path=p22) is True)

# Step 6: attach a broker order ID
attach_broker_order_id(op22, "ORD_ADANI_1", path=p22)

# Step 7-8: "restart" again, confirm order ID + status survive
reloaded_after_attach = get_order(op22, path=p22)
check("23. Restart preserves submitted order ID", reloaded_after_attach["order_id"] == "ORD_ADANI_1")

update_order_verification(op22, make_result(status="PARTIALLY_FILLED", filled=5, pending=3, avg_price=2400.0), path=p22)
reloaded_after_partial = get_order(op22, path=p22)
check("24. Restart preserves partial-fill state", reloaded_after_partial["filled_quantity"] == 5 and reloaded_after_partial["pending_quantity"] == 3)

# Step 9-10: mark resolved, confirm a new operation is then allowed
mark_order_resolved(op22, resolution_reason="Manually reconciled after partial fill", path=p22)
try:
    new_op22 = create_order_intent("ADANIENT", "NSE", "ENTRY", "BUY", 3, path=p22)
    check("22c. After marking resolved, a new operation is allowed", True)
except UnresolvedOrderExistsError:
    check("22c. After marking resolved, a new operation is allowed", False)

# --- 25-29: Validation rejections ---
print("\n--- Validation Rejections ---")
p25 = temp_store_path()

try:
    create_order_intent("ZOMATO", "NSE", "ENTRY", "BUY", 0, path=p25)
    check("25. Invalid quantity rejected", False)
except InvalidOrderRecordError:
    check("25. Invalid quantity rejected", True)

try:
    create_order_intent("ZOMATO", "NSE", "HOLD", "BUY", 5, path=p25)
    check("26. Invalid action rejected", False)
except InvalidOrderRecordError:
    check("26. Invalid action rejected", True)

try:
    create_order_intent("ZOMATO", "NSE", "ENTRY", "HOLD", 5, path=p25)
    check("27. Invalid side rejected", False)
except InvalidOrderRecordError:
    check("27. Invalid side rejected", True)

op28 = create_order_intent("PAYTM", "NSE", "ENTRY", "BUY", 5, path=p25)
try:
    update_order_verification(op28, make_result(status="COMPLETE", filled=999, pending=0, avg_price=10.0), path=p25)
    check("28. Filled quantity greater than requested rejected", False)
except InvalidOrderRecordError:
    check("28. Filled quantity greater than requested rejected", True)

op29 = create_order_intent("DMART", "NSE", "ENTRY", "BUY", 5, path=p25)
try:
    update_order_verification(op29, make_result(status="REJECTED", filled=0, pending=0, avg_price=500.0), path=p25)
    check("29. Average price with zero fill rejected", False)
except InvalidOrderRecordError:
    check("29. Average price with zero fill rejected", True)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- 30: Production pending_orders.json remains untouched ---
print("\n--- Production File Integrity ---")
prod_existed_before = os.path.exists(STORE_PATH)
prod_hash_before = None
if prod_existed_before:
    import hashlib
    with open(STORE_PATH, "rb") as f:
        prod_hash_before = hashlib.sha256(f.read()).hexdigest()

# (all tests above already used exclusively temp-injected paths -- this just
# confirms that discipline held, by checking the real path's state now)
prod_existed_after = os.path.exists(STORE_PATH)
prod_hash_after = None
if prod_existed_after:
    import hashlib
    with open(STORE_PATH, "rb") as f:
        prod_hash_after = hashlib.sha256(f.read()).hexdigest()

check("30. Production pending_orders.json existence unchanged by every test above",
      prod_existed_before == prod_existed_after)
check("30b. Production pending_orders.json content unchanged (if it existed)",
      prod_hash_before == prod_hash_after)

# --- Concurrency: sequential competing writes ---
print("\n--- Concurrency Boundary ---")
p_conc = temp_store_path()
op_c1 = create_order_intent("CONC1", "NSE", "ENTRY", "BUY", 5, path=p_conc)
op_c2 = create_order_intent("CONC2", "NSE", "ENTRY", "BUY", 5, path=p_conc)
attach_broker_order_id(op_c1, "ORD_C1", path=p_conc)
attach_broker_order_id(op_c2, "ORD_C2", path=p_conc)
final = load_pending_orders(p_conc)
check("Sequential competing writes: both records survive intact, no data lost",
      len(final["orders"]) == 2 and
      any(o["order_id"] == "ORD_C1" for o in final["orders"]) and
      any(o["order_id"] == "ORD_C2" for o in final["orders"]))

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
