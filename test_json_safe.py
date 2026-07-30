import numpy as np
import json
from json_safe import json_safe

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

check("numpy.int64 converts to native int", isinstance(json_safe(np.int64(5)), int) and not isinstance(json_safe(np.int64(5)), np.integer))
check("numpy.float64 converts to native float", isinstance(json_safe(np.float64(3.14)), float) and not isinstance(json_safe(np.float64(3.14)), np.floating))
check("numpy.bool_ converts to native bool", isinstance(json_safe(np.bool_(True)), bool) and not isinstance(json_safe(np.bool_(True)), np.bool_))

nested = {"a": np.int64(1), "b": {"c": np.float64(2.5)}, "d": [np.int64(3), np.int64(4)]}
safe_nested = json_safe(nested)
check("Recursively converts nested dicts", isinstance(safe_nested["a"], int))
check("Recursively converts values inside nested dicts", isinstance(safe_nested["b"]["c"], float))
check("Recursively converts values inside lists", all(isinstance(v, int) for v in safe_nested["d"]))

check("Regular Python types pass through unchanged", json_safe("hello") == "hello" and json_safe(42) == 42)
check("None passes through unchanged", json_safe(None) is None)

# The actual real-world reproduction: this exact dict crashed production today
real_incident = {
    "symbol": "MARUTI", "qty": np.int64(9), "support": np.float64(14000.5),
    "resistance": np.int64(14100), "market_structure": np.bool_(True),
}
try:
    json.dumps(json_safe(real_incident))
    check("Real incident reproduction: json.dumps now succeeds", True)
except TypeError:
    check("Real incident reproduction: json.dumps now succeeds", False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
