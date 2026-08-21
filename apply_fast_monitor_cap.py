"""Cap the effective live position-monitor cadence at 5s in fast-exit mode."""
from pathlib import Path

PATH = Path(__file__).resolve().with_name("main.py")


def replace_once(text, old, new, name):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{name}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def patch(text):
    anchor = """    if cfg.ENABLE_CANDLE_ALIGNED_POLLING:\n        logger.info(f\"Candle-aligned polling ENABLED | entry timeframe: {cfg.ENTRY_TIMEFRAME} | \"\n                    f\"position check every {cfg.POSITION_CHECK_SECONDS}s | \"\n                    f\"scan buffer: {cfg.SCAN_BUFFER_SECONDS}s\")\n    scan_guard = ScanGuard()\n"""
    replacement = """    effective_position_check_seconds = float(cfg.POSITION_CHECK_SECONDS)\n    if getattr(cfg, \"ENABLE_FAST_ADVERSE_EXIT\", False):\n        # A stale user_config/launcher override must not silently restore the\n        # old 25s cadence while fast protection is enabled.\n        effective_position_check_seconds = min(effective_position_check_seconds, 5.0)\n\n    if cfg.ENABLE_CANDLE_ALIGNED_POLLING:\n        logger.info(f\"Candle-aligned polling ENABLED | entry timeframe: {cfg.ENTRY_TIMEFRAME} | \"\n                    f\"position check every {effective_position_check_seconds:g}s | \"\n                    f\"scan buffer: {cfg.SCAN_BUFFER_SECONDS}s | \"\n                    f\"fast_adverse={getattr(cfg, 'ENABLE_FAST_ADVERSE_EXIT', False)}\")\n    scan_guard = ScanGuard()\n"""
    if "effective_position_check_seconds" not in text:
        text = replace_once(text, anchor, replacement, "effective monitor cadence")

    sleep_anchor = """            sleep_for = min(cfg.POSITION_CHECK_SECONDS,\n                             max(0, (target_scan_time - datetime.now()).total_seconds()))\n"""
    sleep_replacement = """            sleep_for = min(effective_position_check_seconds,\n                             max(0, (target_scan_time - datetime.now()).total_seconds()))\n"""
    if "min(effective_position_check_seconds" not in text:
        text = replace_once(text, sleep_anchor, sleep_replacement, "monitor sleep cadence")
    return text


if __name__ == "__main__":
    original = PATH.read_text()
    updated = patch(original)
    if updated != original:
        PATH.write_text(updated)
        print("main.py effective fast-monitor cadence applied")
    else:
        print("main.py already caps fast-monitor cadence")
