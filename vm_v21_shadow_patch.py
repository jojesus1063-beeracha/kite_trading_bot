#!/usr/bin/env python3
"""Safely wire EL-BETHEL V2.1 failure-to-launch telemetry into main.py.

This patch is observation-only. It does not add any order, exit, stop,
square-off, cancel, or modify action. It refuses to patch if the expected
main.py anchor is not unique, creates a timestamped backup, and compiles both
files before declaring success.
"""
from __future__ import annotations

import argparse
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main.py"
MODULE = ROOT / "failure_to_launch_shadow.py"

ANCHOR = (
    '    pos = open_positions[symbol]\n'
    '    exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))\n'
)

WIRED = (
    '    pos = open_positions[symbol]\n'
    '    exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))\n'
    '\n'
    '    # FAILURE TO LAUNCH SHADOW -- telemetry only; never exits or changes orders.\n'
    '    try:\n'
    '        from failure_to_launch_shadow import observe_failure_to_launch\n'
    '        _ftl_price = pos.get("current_price") or pos.get("last_price") or pos.get("ltp")\n'
    '        if _ftl_price:\n'
    '            _ftl = observe_failure_to_launch(pos, symbol, _ftl_price)\n'
    '            if _ftl.get("status") in {"FAILURE_TO_LAUNCH_SHADOW", "STRONG_ENTRY_EXCEPTION", "HEALTHY_LAUNCH"}:\n'
    '                logger.warning(\n'
    '                    "FAILURE_TO_LAUNCH_SHADOW | %s | status=%s | mfe=%s | return=%s | adx=%s | di_gap=%s | reason=%s",\n'
    '                    symbol, _ftl.get("status"), _ftl.get("mfe_pct"),\n'
    '                    _ftl.get("current_return_pct"), _ftl.get("entry_adx"),\n'
    '                    _ftl.get("entry_di_gap"), _ftl.get("reason"),\n'
    '                )\n'
    '                save_positions(open_positions)\n'
    '    except Exception as _ftl_exc:\n'
    '        logger.warning("%s: failure-to-launch shadow failed: %s", symbol, _ftl_exc)\n'
)


def patch(text: str) -> tuple[str, list[str]]:
    if "FAILURE TO LAUNCH SHADOW" in text:
        return text, []
    count = text.count(ANCHOR)
    if count != 1:
        raise RuntimeError(f"expected one main.py anchor, found {count}")
    return text.replace(ANCHOR, WIRED, 1), ["wire failure-to-launch shadow telemetry"]


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not MAIN.exists() or not MODULE.exists():
        raise SystemExit("ABORT: main.py and failure_to_launch_shadow.py must both exist")

    original = MAIN.read_text(encoding="utf-8")
    patched, changes = patch(original)

    print("planned_changes =", changes or ["none"])
    print("order_action_strings_in_module =", [
        token for token in ("place_order", "modify_order", "cancel_order", "exit_position", "square_off")
        if token in MODULE.read_text(encoding="utf-8")
    ])

    if args.check:
        print("CHECK ONLY: no files changed")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"main.py.before_v21_shadow_{stamp}"
    shutil.copy2(MAIN, backup)

    try:
        MAIN.write_text(patched, encoding="utf-8")
        py_compile.compile(str(MODULE), doraise=True)
        py_compile.compile(str(MAIN), doraise=True)
    except Exception:
        shutil.copy2(backup, MAIN)
        raise

    print("APPLIED")
    print("backup =", backup.name)
    print("NO service restart performed")
    print("NO broker-order action added")


if __name__ == "__main__":
    main()
