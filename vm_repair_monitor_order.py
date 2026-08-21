#!/usr/bin/env python3
"""Repair ordering of effective_position_check_seconds in the newer VM main.py.

Guarded: only operates when the known bad ordering is present, backs up main.py,
parses before/after, compiles after write, and restores on failure. Never starts
or restarts services.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main.py"

ASSIGN = "    effective_position_check_seconds = float(cfg.POSITION_CHECK_SECONDS)\n"
COMMENT1 = "    # Live hardening: position reconciliation/exit checks must not be\n"
COMMENT2 = "    # restored to the old ~25s cadence by stale config overrides.\n"
CAP = "    effective_position_check_seconds = min(effective_position_check_seconds, 5.0)\n"
LOG_TOKEN = 'f"position check every {effective_position_check_seconds:g}s | "'


def main() -> None:
    if not MAIN.exists():
        raise SystemExit("ABORT: main.py not found")
    text = MAIN.read_text(encoding="utf-8")
    ast.parse(text, filename=str(MAIN))

    for token, label in ((ASSIGN, "assignment"), (CAP, "cap"), (LOG_TOKEN, "startup log")):
        count = text.count(token)
        if count != 1:
            raise SystemExit(f"ABORT: expected exactly one {label}, found {count}")

    assign_pos = text.index(ASSIGN)
    cap_pos = text.index(CAP)
    log_pos = text.index(LOG_TOKEN)

    print("assignment_before_log =", assign_pos < log_pos)
    print("cap_before_log        =", cap_pos < log_pos)

    if assign_pos < log_pos and cap_pos < log_pos:
        print("OK: ordering already safe; no changes made")
        return

    # Find the logger.info( call that owns the startup f-string.  Insert the
    # effective cadence block immediately before the logger call, not inside
    # its argument list.
    logger_start = text.rfind("            logger.info(", 0, log_pos)
    if logger_start == -1:
        logger_start = text.rfind("        logger.info(", 0, log_pos)
    if logger_start == -1:
        logger_start = text.rfind("    logger.info(", 0, log_pos)
    if logger_start == -1:
        raise SystemExit("ABORT: could not find owning logger.info() before startup log")

    # Remove the existing hardening block wherever it currently sits.
    block_variants = [
        ASSIGN + COMMENT1 + COMMENT2 + CAP + "\n",
        ASSIGN + COMMENT1 + COMMENT2 + CAP,
        ASSIGN + CAP + "\n",
        ASSIGN + CAP,
    ]
    removed = False
    for block in block_variants:
        if block in text:
            text = text.replace(block, "", 1)
            removed = True
            break
    if not removed:
        raise SystemExit("ABORT: effective cadence block shape not recognized")

    # Recompute the logger anchor after removal.
    log_pos = text.index(LOG_TOKEN)
    logger_start = text.rfind("            logger.info(", 0, log_pos)
    if logger_start == -1:
        logger_start = text.rfind("        logger.info(", 0, log_pos)
    if logger_start == -1:
        logger_start = text.rfind("    logger.info(", 0, log_pos)
    if logger_start == -1:
        raise SystemExit("ABORT: logger.info() anchor disappeared after block removal")

    safe_block = ASSIGN + COMMENT1 + COMMENT2 + CAP + "\n"
    patched = text[:logger_start] + safe_block + text[logger_start:]
    ast.parse(patched, filename=str(MAIN))

    # Sanity: both definitions must now precede the startup f-string.
    if not (patched.index(ASSIGN) < patched.index(LOG_TOKEN) and patched.index(CAP) < patched.index(LOG_TOKEN)):
        raise SystemExit("ABORT: ordering repair did not produce safe order")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = MAIN.with_name(f"main.py.before_monitor_order_fix_{stamp}")
    shutil.copy2(MAIN, backup)
    try:
        MAIN.write_text(patched, encoding="utf-8")
        subprocess.run([sys.executable, "-m", "py_compile", str(MAIN)], cwd=ROOT, check=True)
    except Exception:
        shutil.copy2(backup, MAIN)
        raise

    print("REPAIRED: effective monitor cadence is defined before startup log")
    print("backup =", backup.name)
    print("No service restart was performed")


if __name__ == "__main__":
    main()
