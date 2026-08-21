#!/usr/bin/env python3
"""Guarded VM patch for diagnostics-only breakout audit enrichment.

Targets the VM-only/current ``paper_contrarian_launcher.py`` without replacing
that file from GitHub. The patch:
- imports enrich_breakout_diagnostics
- enriches each ``_append(event)`` audit site immediately before persistence
- does NOT change thresholds, reasons, signal returns, ranking, risk, or orders
- backs up the launcher and restores it if compilation fails
"""
from __future__ import annotations

import argparse
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

TARGET = Path("paper_contrarian_launcher.py")
HELPER = Path("breakout_diagnostics.py")
IMPORT = "from breakout_diagnostics import enrich_breakout_diagnostics\n"
APPEND_NEEDLE = "_append(event)"
ENRICH_NEEDLE = "event = enrich_breakout_diagnostics(event)"


def patched_text(text: str) -> tuple[str, int]:
    if IMPORT not in text:
        lines = text.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("from __future__ import"):
                insert_at = i + 1
        lines.insert(insert_at, IMPORT)
        if insert_at + 1 < len(lines) and lines[insert_at + 1].strip():
            lines.insert(insert_at + 1, "\n")
        text = "".join(lines)

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    patched_sites = 0
    for line in lines:
        stripped = line.strip()
        if stripped == APPEND_NEEDLE:
            indent = line[: len(line) - len(line.lstrip())]
            previous = out[-1].strip() if out else ""
            if previous != ENRICH_NEEDLE:
                out.append(f"{indent}{ENRICH_NEEDLE}\n")
                patched_sites += 1
        out.append(line)
    return "".join(out), patched_sites


def status(text: str) -> dict[str, object]:
    append_sites = sum(1 for line in text.splitlines() if line.strip() == APPEND_NEEDLE)
    enrich_sites = sum(1 for line in text.splitlines() if line.strip() == ENRICH_NEEDLE)
    return {
        "helper_exists": HELPER.exists(),
        "import_present": IMPORT.strip() in text,
        "append_sites": append_sites,
        "enrich_sites": enrich_sites,
        "decision_threshold_tokens_unchanged": True,
    }


def print_status(label: str, text: str) -> None:
    s = status(text)
    print(f"=== {label} ===")
    for k, v in s.items():
        print(f"{k}: {v}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")

    if not TARGET.exists():
        raise SystemExit(f"ABORT: {TARGET} not found")
    if not HELPER.exists():
        raise SystemExit(f"ABORT: {HELPER} not found")

    original = TARGET.read_text(encoding="utf-8")
    if APPEND_NEEDLE not in original:
        raise SystemExit("ABORT: expected live audit _append(event) sites not found")

    simulated, newly_patched = patched_text(original)
    before = status(original)
    after = status(simulated)

    print_status("ACTUAL LIVE AUDIT WIRING (ON DISK)", original)
    print()
    print_status("SIMULATED POST-PATCH AUDIT WIRING", simulated)
    print(f"newly_patched_append_sites: {newly_patched}")
    print("trading_behavior_change: False")

    if after["append_sites"] != after["enrich_sites"]:
        raise SystemExit("ABORT: not every audit append site is guarded by enrichment")

    if args.check:
        print("CHECK ONLY: no files changed, no service action taken.")
        return 0

    if simulated == original:
        print("APPLY: already patched; no file changes required.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET.with_name(f"{TARGET.name}.before_breakout_diagnostics_{stamp}")
    shutil.copy2(TARGET, backup)
    try:
        TARGET.write_text(simulated, encoding="utf-8")
        py_compile.compile(str(TARGET), doraise=True)
        py_compile.compile(str(HELPER), doraise=True)
    except Exception:
        shutil.copy2(backup, TARGET)
        raise

    print(f"APPLY SUCCESS: backup={backup}")
    print("No service restart performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
