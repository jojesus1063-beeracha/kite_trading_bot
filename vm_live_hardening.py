#!/usr/bin/env python3
"""Guarded migration for the newer Ubuntu live architecture.

This script is intentionally VM-aware and conservative. It NEVER starts or
restarts systemd and NEVER places an order. It only patches settings/monitor
cadence that can be identified unambiguously in the live tree.

Usage on the trading VM:
    python3 vm_live_hardening.py --check
    python3 vm_live_hardening.py --apply
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / "combined_live_launcher.py"
MAIN = ROOT / "main.py"
CONFIG = ROOT / "config.py"

TARGETS = {
    "LIVE_RISK_PER_TRADE_PCT": "0.20",
    "LIVE_MAX_OPEN_POSITIONS": "1",
    "LIVE_MAX_TRADES_PER_DAY": "5",
    "LIVE_MAX_DAILY_LOSS_PCT": "0.50",
    "LIVE_MAX_POSITION_SIZE_PCT": "50.0",
}
FAST_MONITOR_SECONDS = 5.0


def _assignment_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _replace_assignment(text: str, name: str, value: str) -> tuple[str, bool]:
    pattern = re.compile(rf"^(?P<indent>\s*){re.escape(name)}\s*=\s*[^#\n]+(?P<comment>\s*#.*)?$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, False
    if len(matches) != 1:
        raise RuntimeError(f"{name}: expected one assignment, found {len(matches)}")
    m = matches[0]
    comment = m.group("comment") or ""
    replacement = f"{m.group('indent')}{name} = {value}{comment}"
    return text[:m.start()] + replacement + text[m.end():], True


def _patch_launcher(text: str) -> tuple[str, list[str]]:
    changed: list[str] = []
    for name, value in TARGETS.items():
        text2, found = _replace_assignment(text, name, value)
        if found:
            if text2 != text:
                changed.append(f"{name} -> {value}")
            text = text2
    for required in ("LIVE_MAX_OPEN_POSITIONS", "LIVE_MAX_TRADES_PER_DAY"):
        if required not in text:
            raise RuntimeError(f"launcher anchor missing: {required}")
    return text, changed


def _patch_main_monitor(text: str) -> tuple[str, list[str]]:
    changed: list[str] = []
    if "effective_position_check_seconds" not in text:
        anchor = "    scan_guard = ScanGuard()\n"
        if text.count(anchor) != 1:
            raise RuntimeError("main.py: ScanGuard anchor not found exactly once")
        block = (
            "    effective_position_check_seconds = float(cfg.POSITION_CHECK_SECONDS)\n"
            "    # Live hardening: cap position reconciliation/exit checks at 5s.\n"
            f"    effective_position_check_seconds = min(effective_position_check_seconds, {FAST_MONITOR_SECONDS:.1f})\n\n"
        )
        text = text.replace(anchor, block + anchor, 1)
        changed.append("insert effective 5s position-monitor cap")

    old = "sleep_for = min(cfg.POSITION_CHECK_SECONDS,"
    new = "sleep_for = min(effective_position_check_seconds,"
    if old in text:
        if text.count(old) != 1:
            raise RuntimeError(f"main.py: expected one monitor sleep anchor, found {text.count(old)}")
        text = text.replace(old, new, 1)
        changed.append("position monitor sleep -> effective <=5s")
    elif new not in text:
        raise RuntimeError("main.py: position-monitor sleep anchor not recognized")

    log_old = 'f"position check every {cfg.POSITION_CHECK_SECONDS}s | "'
    log_new = 'f"position check every {effective_position_check_seconds:g}s | "'
    if log_old in text:
        text = text.replace(log_old, log_new, 1)
        changed.append("startup log -> effective position cadence")
    return text, changed


def _read_assignment(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\s*=\s*([^#\n]+)", text, re.MULTILINE)
    return m.group(1).strip() if m else "<NOT FOUND>"


def _report_launcher(title: str, text: str) -> None:
    print(f"\n=== {title} ===")
    for name, wanted in TARGETS.items():
        actual = _read_assignment(text, name)
        status = "OK" if actual == wanted else "CHANGE"
        print(f"{status:6} {name:32} actual={actual:12} target={wanted}")


def _report_main(title: str, text: str) -> None:
    print(f"\n=== {title} ===")
    print("3-minute architecture present:", "3minute" in text or "ENTRY_TIMEFRAME" in text)
    print("effective monitor cap present:", "effective_position_check_seconds" in text)
    print("monitor sleep uses cap:", "sleep_for = min(effective_position_check_seconds," in text)
    print("candidate ranking present:", "Candidate ranking" in text or "valid_candidates" in text)
    print("broker protective-stop code present:", "protective" in text.lower() and "stop" in text.lower())


def _backup(path: Path, stamp: str) -> Path:
    backup = path.with_name(f"{path.name}.before_stewardship_{stamp}")
    shutil.copy2(path, backup)
    return backup


def _compile(paths: list[Path]) -> None:
    cmd = [sys.executable, "-m", "py_compile", *[str(p) for p in paths if p.exists()]]
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    missing = [p.name for p in (LAUNCHER, MAIN, CONFIG) if not p.exists()]
    if missing:
        raise SystemExit(f"ABORT: expected live files missing: {missing}")

    launcher_original = LAUNCHER.read_text(encoding="utf-8")
    main_original = MAIN.read_text(encoding="utf-8")
    _assignment_names(LAUNCHER)
    ast.parse(main_original, filename=str(MAIN))

    launcher_patched, launcher_changes = _patch_launcher(launcher_original)
    main_patched, main_changes = _patch_main_monitor(main_original)

    # Critical fix: report ACTUAL disk state first, then separately show the
    # simulated post-patch state. Older versions mistakenly reported only the
    # simulated state, making --check look as if changes were already applied.
    _report_launcher("ACTUAL LIVE LAUNCHER STATE (ON DISK)", launcher_original)
    _report_main("ACTUAL MAIN LATENCY STATE (ON DISK)", main_original)

    print("\n=== PLANNED CHANGES ===")
    for item in launcher_changes + main_changes:
        print("-", item)
    if not launcher_changes and not main_changes:
        print("- none; files already match guarded patch")

    _report_launcher("SIMULATED POST-PATCH LAUNCHER STATE", launcher_patched)
    _report_main("SIMULATED POST-PATCH MAIN STATE", main_patched)

    unresolved = []
    for name in ("LIVE_RISK_PER_TRADE_PCT", "LIVE_MAX_DAILY_LOSS_PCT", "LIVE_MAX_POSITION_SIZE_PCT"):
        if re.search(rf"^{re.escape(name)}\s*=", launcher_patched, re.MULTILINE) is None:
            unresolved.append(name)
    if unresolved:
        print("\nREVIEW REQUIRED: launcher uses different names for:", ", ".join(unresolved))
        print("No guessed replacement will be made for those settings.")
        if args.apply:
            raise SystemExit("ABORT: unresolved live risk constant names")

    if args.check:
        print("\nCHECK ONLY: no files changed, no service action taken.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups: list[Path] = []
    try:
        backups.append(_backup(LAUNCHER, stamp))
        backups.append(_backup(MAIN, stamp))
        LAUNCHER.write_text(launcher_patched, encoding="utf-8")
        MAIN.write_text(main_patched, encoding="utf-8")
        _compile([LAUNCHER, MAIN, CONFIG])
    except Exception:
        for backup in backups:
            original = backup.name.split(".before_stewardship_", 1)[0]
            shutil.copy2(backup, ROOT / original)
        raise

    print("\nAPPLIED SAFELY. Backups:")
    for backup in backups:
        print("-", backup.name)
    print("\nNo service restart was performed.")
    print("Verify next:")
    print("  python3 -m py_compile combined_live_launcher.py main.py config.py entry_timing.py")
    print("  python3 vm_live_hardening.py --check")
    print("  grep -nE 'LIVE_(RISK|MAX_OPEN|MAX_TRADES|MAX_DAILY|MAX_POSITION)' combined_live_launcher.py")
    print("  grep -nE 'POSITION_CHECK_SECONDS|effective_position_check_seconds|sleep_for' main.py config.py")


if __name__ == "__main__":
    main()
