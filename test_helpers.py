"""
Shared test-isolation utility. Every real test that needs to call the
ACTUAL record_trade()/save_bot_status()/load_bot_status()/save_positions()/
load_positions() (not a mock) must use this, so it can never write to
the real production trade_history.jsonl, bot_status.json, or
open_positions.json -- the exact incident this exists to prevent
happened twice tonight before this was built.

Uses the injectable-path parameters those functions now accept
(log_path/status_path/positions_path) rather than monkeypatching --
each test gets its own isolated temp directory, cleaned up
automatically.
"""
import os
import shutil
import tempfile
import contextlib


@contextlib.contextmanager
def isolated_runtime_paths():
    """
    Yields an object with .log_path, .status_path, .positions_path --
    real, writable paths inside a fresh temp directory, guaranteed
    distinct from the production paths. Directory is removed on exit
    regardless of whether the test passed, failed, or raised.
    """
    tmpdir = tempfile.mkdtemp(prefix="kitebot_test_")

    class Paths:
        log_path = os.path.join(tmpdir, "trade_history.jsonl")
        status_path = os.path.join(tmpdir, "bot_status.json")
        positions_path = os.path.join(tmpdir, "open_positions.json")

    try:
        yield Paths()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
