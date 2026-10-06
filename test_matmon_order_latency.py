from pathlib import Path


def test_latency_configuration_is_time_critical():
    source = Path("config.py").read_text()
    assert "CANDLE_COMPLETION_BUFFER_SECONDS = 0" in source
    assert "SCAN_BUFFER_SECONDS = 2" in source
    assert "ENTRY_SIGNAL_MAX_AGE_SECONDS = 8.0" in source


def test_live_entry_path_has_stale_signal_guard():
    source = Path("main.py").read_text()
    assert "STALE_SIGNAL_BLOCKED" in source
    assert "ENTRY_SIGNAL_MAX_AGE_SECONDS" in source
    assert "stale signal blocked before order submission" in source


def test_broker_submission_latency_is_instrumented():
    source = Path("executor.py").read_text()
    assert "BROKER_ENTRY_SUBMIT_LATENCY" in source
    assert "broker_submit_started = time.monotonic()" in source
