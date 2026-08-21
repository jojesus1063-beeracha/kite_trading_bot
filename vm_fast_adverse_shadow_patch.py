#!/usr/bin/env python3
"""Guardedly wire fast_adverse_shadow into the newer Ubuntu live main.py.

Safety properties:
- shadow telemetry only; never adds an exit trigger or order call
- reuses the already-running ws_shadow_engine TickBuffer
- stale/missing ticks fail closed to observation-only skips
- backs up main.py and restores it on compile failure
- refuses to patch if expected VM anchors are not unique
"""
from __future__ import annotations

import argparse
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main.py"
MODULE = ROOT / "fast_adverse_shadow.py"

ENGINE_ANCHOR = "        ws_shadow_engine = start_ws_shadow_engine(kite, symbols, tokens, exchange_map)\n"
ENGINE_WIRED = (
    "        ws_shadow_engine = start_ws_shadow_engine(kite, symbols, tokens, exchange_map)\n"
    "        # Shadow-only fast adverse telemetry may read the existing WS tick buffer.\n"
    "        # This does NOT enable WS candles for signals and does NOT place orders.\n"
    "        globals()[\"_FAST_ADVERSE_WS_ENGINE\"] = ws_shadow_engine\n"
)

CHECK_ANCHOR = (
    "    pos = open_positions[symbol]\n"
    "    exchange = pos.get(\"exchange\", exchange_map.get(symbol, \"NSE\"))\n"
    "    resumed_exit_reason = None\n"
)
CHECK_WIRED = (
    "    pos = open_positions[symbol]\n"
    "    exchange = pos.get(\"exchange\", exchange_map.get(symbol, \"NSE\"))\n"
    "\n"
    "    # Shadow-only fast adverse observer. It may mutate/persist telemetry fields\n"
    "    # on the position, but it cannot request an exit or touch broker orders.\n"
    "    try:\n"
    "        _fa_engine = globals().get(\"_FAST_ADVERSE_WS_ENGINE\")\n"
    "        _fa_ticker = getattr(_fa_engine, \"ws_ticker\", None)\n"
    "        _fa_buffer = getattr(_fa_ticker, \"tick_buffer\", None)\n"
    "        _fa_tick = _fa_buffer.latest(symbol) if _fa_buffer is not None else None\n"
    "        from fast_adverse_shadow import observe_fast_adverse_shadow\n"
    "        _fa_event = observe_fast_adverse_shadow(pos, symbol, _fa_tick)\n"
    "        _fa_status = _fa_event.get(\"status\")\n"
    "        if _fa_status in {\"ARMED\", \"DISARMED\", \"WOULD_EXIT\"}:\n"
    "            if not (\n"
    "                _fa_status == \"WOULD_EXIT\"\n"
    "                and _fa_event.get(\"reason\") == \"shadow trigger already confirmed\"\n"
    "            ):\n"
    "                logger.warning(\n"
    "                    \"FAST ADVERSE SHADOW | %s | status=%s | price=%s | \"\n"
    "                    \"adverse_r=%s | tick_age=%s | reason=%s\",\n"
    "                    symbol, _fa_status, _fa_event.get(\"live_price\"),\n"
    "                    _fa_event.get(\"adverse_r\"), _fa_event.get(\"tick_age_seconds\"),\n"
    "                    _fa_event.get(\"reason\"),\n"
    "                )\n"
    "            save_positions(open_positions)\n"
    "    except Exception as _fa_exc:\n"
    "        logger.warning(\"%s: fast adverse shadow observation failed: %s\", symbol, _fa_exc)\n"
    "\n"
    "    resumed_exit_reason = None\n"
)


def patch(text: str) -> tuple[str, list[str]]:
    changes = []
    if 'globals()["_FAST_ADVERSE_WS_ENGINE"] = ws_shadow_engine' not in text:
        if text.count(ENGINE_ANCHOR) != 1:
            raise RuntimeError(
                f"WS engine anchor expected once, found {text.count(ENGINE_ANCHOR)}"
            )
        text = text.replace(ENGINE_ANCHOR, ENGINE_WIRED, 1)
        changes.append("retain ws_shadow_engine for shadow LTP telemetry")

    if "FAST ADVERSE SHADOW | %s" not in text:
        if text.count(CHECK_ANCHOR) != 1:
            raise RuntimeError(
                f"check_position_exit anchor expected once, found {text.count(CHECK_ANCHOR)}"
            )
        text = text.replace(CHECK_ANCHOR, CHECK_WIRED, 1)
        changes.append("observe latest WS tick in check_position_exit (shadow only)")

    return text, changes


def report(text: str) -> None:
    print("fast adverse module present:", MODULE.exists())
    print("WS engine retained for telemetry:", 'globals()["_FAST_ADVERSE_WS_ENGINE"] = ws_shadow_engine' in text)
    print("shadow observer wired:", "FAST ADVERSE SHADOW | %s" in text)
    print("live fast-exit trigger added:", 'result = "fast_adverse_exit"' in text)
    print("WS candle mode changed here:", "WS_CANDLE_MODE" in text and "fast adverse" in text)


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not MAIN.exists() or not MODULE.exists():
        raise SystemExit("ABORT: main.py and fast_adverse_shadow.py must both exist")

    original = MAIN.read_text(encoding="utf-8")
    patched, changes = patch(original)

    print("=== ACTUAL STATE ===")
    report(original)
    print("\n=== PLANNED CHANGES ===")
    if changes:
        for change in changes:
            print("-", change)
    else:
        print("- none; wiring already present")
    print("\n=== SIMULATED POST-PATCH STATE ===")
    report(patched)

    if args.check:
        print("\nCHECK ONLY: no files changed and no service action taken.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"main.py.before_fast_adverse_shadow_{stamp}"
    shutil.copy2(MAIN, backup)
    try:
        MAIN.write_text(patched, encoding="utf-8")
        py_compile.compile(str(MODULE), doraise=True)
        py_compile.compile(str(MAIN), doraise=True)
    except Exception:
        shutil.copy2(backup, MAIN)
        raise

    print("\nAPPLIED SHADOW WIRING SAFELY")
    print("backup =", backup.name)
    print("NO service restart performed")
    print("NO live exit trigger was added")
    print("NO order-placement code was added")


if __name__ == "__main__":
    main()
