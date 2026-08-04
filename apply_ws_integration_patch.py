#!/usr/bin/env python3
"""
Safely patches main.py to add the WS shadow engine hook, WITHOUT
replacing the whole file. Run this from the repo root (or a worktree
root) that already has ws_ticker.py, candle_engine.py,
indicators_incremental.py, entry_pricing.py, ws_integration.py present.

This finds two exact anchor blocks and edits around them. If either
anchor isn't found EXACTLY as expected (e.g. because main.py changed
again since this script was written), it aborts with a clear error
and touches nothing -- it will never silently drop unrelated changes
like PR #8's latency work.

Usage:
    python3 apply_ws_integration_patch.py main.py
"""
import sys

ANCHOR_1_OLD = '''    tokens = {s: get_instrument_token(kite, s, exchange_map[s]) for s in symbols}

    # --- Restore any open positions from before a crash/restart today ---'''

ANCHOR_1_NEW = '''    tokens = {s: get_instrument_token(kite, s, exchange_map[s]) for s in symbols}

    # --- Optional WS shadow engine (opt-in, cfg.ENABLE_WS_CANDLES) ---
    # Runs entirely independently of the REST-based loop below via its
    # own background thread; never touches run_full_scan(), evaluate(),
    # or order placement. See ws_integration.py. A no-op (returns None
    # immediately) when cfg.ENABLE_WS_CANDLES is False, the default.
    ws_shadow_engine = None
    if getattr(cfg, "ENABLE_WS_CANDLES", False):
        from ws_integration import start_ws_shadow_engine
        ws_shadow_engine = start_ws_shadow_engine(kite, symbols, tokens, exchange_map)

    # --- Restore any open positions from before a crash/restart today ---'''

ANCHOR_2_OLD = '''    scan_guard = ScanGuard()

    while True:
        now = datetime.now()'''

ANCHOR_2_NEW = '''    scan_guard = ScanGuard()

    try:
        while True:
            now = datetime.now()'''


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 apply_ws_integration_patch.py main.py")
        sys.exit(1)

    path = sys.argv[1]
    with open(path) as f:
        content = f.read()

    if content.count(ANCHOR_1_OLD) != 1:
        print(f"ABORT: anchor 1 (tokens/open_positions boundary) found "
              f"{content.count(ANCHOR_1_OLD)} time(s), expected exactly 1.")
        print("main.py has likely changed since this patch was written -- "
              "do NOT proceed blindly. Send the current main.py content "
              "back for a fresh patch instead.")
        sys.exit(1)

    if content.count(ANCHOR_2_OLD) != 1:
        print(f"ABORT: anchor 2 (scan_guard/while True boundary) found "
              f"{content.count(ANCHOR_2_OLD)} time(s), expected exactly 1.")
        print("main.py has likely changed since this patch was written -- "
              "do NOT proceed blindly. Send the current main.py content "
              "back for a fresh patch instead.")
        sys.exit(1)

    content = content.replace(ANCHOR_1_OLD, ANCHOR_1_NEW)
    content = content.replace(ANCHOR_2_OLD, ANCHOR_2_NEW)

    # Reindent the while-loop body (everything from "now = datetime.now()"
    # up to, but not including, "if __name__") by 4 spaces, and add the
    # finally block, mirroring exactly what was manually verified via
    # diff during development.
    lines = content.split("\n")
    while_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "while True:" and "            now = datetime.now()" in lines[i + 1]:
            while_idx = i
            break
    if while_idx is None:
        print("ABORT: could not locate the 'while True:' block after anchor patching. "
              "No changes written.")
        sys.exit(1)

    main_idx = None
    for i in range(while_idx, len(lines)):
        if lines[i].startswith("if __name__"):
            main_idx = i
            break
    if main_idx is None:
        print("ABORT: could not locate 'if __name__' after the while loop. No changes written.")
        sys.exit(1)

    new_lines = lines[:while_idx + 2]  # "while True:" + the already-patched "now = ..." line
    for line in lines[while_idx + 2:main_idx]:
        if line.strip() == "":
            new_lines.append(line)
        else:
            new_lines.append("    " + line)
    new_lines.append("    finally:")
    new_lines.append("        if ws_shadow_engine is not None:")
    new_lines.append("            ws_shadow_engine.stop()")
    new_lines.append("")
    new_lines += lines[main_idx:]

    with open(path, "w") as f:
        f.write("\n".join(new_lines))

    print(f"OK: patched {path}. Run `python3 -m py_compile {path}` and your test suite next.")


if __name__ == "__main__":
    main()
