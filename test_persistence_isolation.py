"""
Proves the test-isolation mechanism actually works: real calls to
record_trade()/save_bot_status()/save_positions() using injected temp
paths never touch the real production files. Does NOT run the full
test suite from within a test (that's a separate, explicit script --
see the regression run instructions) -- this file only exercises the
isolation mechanism itself, directly.
"""
import os
from test_helpers import isolated_runtime_paths
from trade_log import record_trade, save_bot_status, load_bot_status, get_trade_history
from position_store import save_positions, load_positions, POSITIONS_PATH
from trade_log import LOG_PATH, STATUS_PATH

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

# Snapshot whether the real production files exist BEFORE these tests run
real_status_existed_before = os.path.exists(STATUS_PATH)
real_positions_existed_before = os.path.exists(POSITIONS_PATH)
real_log_size_before = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else None

with isolated_runtime_paths() as paths:
    # --- record_trade with injected path ---
    record_trade("ISOTEST", "BUY", 10, 100.0, 105.0, 50.0, "target",
                 log_path=paths.log_path)
    check("record_trade writes to the injected temp path, not production",
          os.path.exists(paths.log_path) and not (
              os.path.exists(LOG_PATH) and "ISOTEST" in open(LOG_PATH).read()
          ))
    history = get_trade_history(log_path=paths.log_path)
    check("get_trade_history reads back from the injected path correctly",
          len(history) == 1 and history[0]["symbol"] == "ISOTEST")

    # --- save_bot_status with injected path ---
    save_bot_status(["dummy"], positions=[{"symbol": "ISOTEST"}],
                    status_path=paths.status_path)
    check("save_bot_status writes to the injected temp path", os.path.exists(paths.status_path))
    check("Production bot_status.json was NOT created/modified by this test",
          os.path.exists(STATUS_PATH) == real_status_existed_before)
    loaded_status = load_bot_status(status_path=paths.status_path)
    check("load_bot_status reads back from the injected path correctly",
          loaded_status is not None and loaded_status["positions"][0]["symbol"] == "ISOTEST")
    check("snapshot_id present in the written status", "snapshot_id" in loaded_status)
    check("generated_at present in the written status", "generated_at" in loaded_status)

    # --- save_positions with injected path ---
    save_positions({"ISOTEST": {"direction": "BUY", "qty": 10}}, positions_path=paths.positions_path)
    check("save_positions writes to the injected temp path", os.path.exists(paths.positions_path))
    check("Production open_positions.json was NOT created/modified by this test",
          os.path.exists(POSITIONS_PATH) == real_positions_existed_before)
    reloaded_positions = load_positions(positions_path=paths.positions_path)
    check("load_positions reads back from the injected path correctly", "ISOTEST" in reloaded_positions)

# --- After the context manager exits, temp dir is cleaned up ---
check("Temp directory is cleaned up after the isolated block exits", not os.path.exists(paths.status_path))

# --- Final check: production files still match their before-state ---
check("Production trade_history.jsonl size unchanged after all isolation tests",
      (os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else None) == real_log_size_before)
check("Production bot_status.json existence unchanged", os.path.exists(STATUS_PATH) == real_status_existed_before)
check("Production open_positions.json existence unchanged", os.path.exists(POSITIONS_PATH) == real_positions_existed_before)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
