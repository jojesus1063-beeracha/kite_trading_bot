import vm_live_hardening as vm


def test_launcher_live_limits_are_hardened_without_touching_other_code():
    src = '''
LIVE_RISK_PER_TRADE_PCT = 2.0
LIVE_MAX_OPEN_POSITIONS = 1
LIVE_MAX_TRADES_PER_DAY = 7
LIVE_MAX_DAILY_LOSS_PCT = 0.5
LIVE_MAX_POSITION_SIZE_PCT = 100.0
OTHER_LIVE_SETTING = 42
'''
    patched, changes = vm._patch_launcher(src)
    assert "LIVE_RISK_PER_TRADE_PCT = 0.20" in patched
    assert "LIVE_MAX_OPEN_POSITIONS = 1" in patched
    assert "LIVE_MAX_TRADES_PER_DAY = 5" in patched
    assert "LIVE_MAX_DAILY_LOSS_PCT = 0.50" in patched
    assert "LIVE_MAX_POSITION_SIZE_PCT = 50.0" in patched
    assert "OTHER_LIVE_SETTING = 42" in patched
    assert changes


def test_launcher_does_not_guess_unknown_risk_constant_names():
    src = '''
LIVE_MAX_OPEN_POSITIONS = 1
LIVE_MAX_TRADES_PER_DAY = 7
SOME_DIFFERENT_RISK_NAME = 2.0
'''
    patched, _ = vm._patch_launcher(src)
    assert "SOME_DIFFERENT_RISK_NAME = 2.0" in patched
    assert "LIVE_MAX_TRADES_PER_DAY = 5" in patched


def test_main_monitor_patch_caps_sleep_and_preserves_strategy_markers():
    src = '''
class ScanGuard:
    pass

class cfg:
    POSITION_CHECK_SECONDS = 25

# ENTRY_TIMEFRAME = 3minute
# Candidate ranking complete
# protective stop

def run():
    scan_guard = ScanGuard()
    while True:
        sleep_for = min(cfg.POSITION_CHECK_SECONDS,
                        100)
        break
'''
    patched, changes = vm._patch_main_monitor(src)
    assert "effective_position_check_seconds = float(cfg.POSITION_CHECK_SECONDS)" in patched
    assert "min(effective_position_check_seconds," in patched
    assert "3minute" in patched
    assert "Candidate ranking complete" in patched
    assert "protective stop" in patched
    assert changes


def test_main_monitor_patch_is_idempotent():
    src = '''
class ScanGuard:
    pass

class cfg:
    POSITION_CHECK_SECONDS = 25

def run():
    effective_position_check_seconds = float(cfg.POSITION_CHECK_SECONDS)
    effective_position_check_seconds = min(effective_position_check_seconds, 5.0)

    scan_guard = ScanGuard()
    while True:
        sleep_for = min(effective_position_check_seconds,
                        100)
        break
'''
    patched, changes = vm._patch_main_monitor(src)
    assert patched == src
    assert changes == []
