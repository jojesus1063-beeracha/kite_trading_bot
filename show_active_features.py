"""
Displays every feature flag and safety setting currently active,
reading the EFFECTIVE values from config.py (after any user_config.json
overrides are applied -- not just the hardcoded source defaults).

Read-only. Does not modify anything, does not touch Kite, does not
touch trading. Safe to run any time, including while the bot is live.

Usage:
    python3 show_active_features.py
"""

import os
import subprocess
import sys

import config as cfg

CRITICAL_FLAGS = [
    "PAPER_TRADING",
    "ENABLE_WS_CANDLES",
    "WS_CANDLE_MODE",
    "ENABLE_CANDLE_ALIGNED_POLLING",
]


def _section(title):
    print(f"\n{'=' * 70}")
    print(title)
    print("=" * 70)


def show_context():
    _section("CONTEXT")
    print(f"Working directory: {os.getcwd()}")

    try:
        branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5)
        commit = subprocess.run(["git", "log", "-1", "--oneline"], capture_output=True, text=True, timeout=5)
        print(f"Git branch: {branch.stdout.strip() or '(detached HEAD)'}")
        print(f"Git commit: {commit.stdout.strip()}")
    except Exception as e:
        print(f"Git info unavailable: {e}")

    try:
        status = subprocess.run(["systemctl", "is-active", "kitebot.service"], capture_output=True, text=True, timeout=5)
        print(f"kitebot.service status: {status.stdout.strip()}")
    except Exception as e:
        print(f"Could not check kitebot.service status: {e}")

    override_path = getattr(cfg, "_USER_CONFIG_PATH", None)
    if override_path and os.path.exists(override_path):
        print(f"\nNOTE: {override_path} exists and may override some settings below.")
        print("The values shown are the EFFECTIVE values (after any override is applied),")
        print("not necessarily what's hardcoded in config.py's source.")
    else:
        print(f"\nNo user_config.json override file found -- all values below are config.py's hardcoded defaults.")


def show_critical_flags():
    _section("CRITICAL SAFETY FLAGS (check these first)")
    for name in CRITICAL_FLAGS:
        val = getattr(cfg, name, "(not defined)")
        flag = ""
        if name == "PAPER_TRADING" and val is not True:
            flag = "  <-- WARNING: NOT in paper mode, real orders can be placed"
        if name == "ENABLE_WS_CANDLES" and val is True:
            flag = "  (WS candle engine feature is ON)"
        if name == "WS_CANDLE_MODE" and val == "live":
            flag = "  (WS candles CAN influence real signals, not just logging)"
        print(f"  {name} = {val}{flag}")


def show_all_boolean_flags():
    _section("ALL BOOLEAN FEATURE FLAGS (from config.py)")
    names = sorted(n for n in dir(cfg) if not n.startswith("_"))
    on, off = [], []
    for name in names:
        val = getattr(cfg, name)
        if isinstance(val, bool):
            (on if val else off).append(name)

    print(f"\n-- ON ({len(on)}) --")
    for name in on:
        print(f"  {name} = True")

    print(f"\n-- OFF ({len(off)}) --")
    for name in off:
        print(f"  {name} = False")


def show_mode_and_threshold_settings():
    _section("MODE / THRESHOLD SETTINGS (non-boolean, likely to matter)")
    names = sorted(n for n in dir(cfg) if not n.startswith("_"))
    interesting_suffixes = ("_MODE", "_PCT", "_THRESHOLD", "_LIMIT", "_TIMEFRAME", "_PERIOD")
    for name in names:
        if name in CRITICAL_FLAGS:
            continue  # already shown above
        val = getattr(cfg, name)
        if isinstance(val, (int, float, str)) and any(name.endswith(s) for s in interesting_suffixes):
            print(f"  {name} = {val!r}")


def show_watchlist_summary():
    _section("WATCHLIST")
    watchlist = getattr(cfg, "WATCHLIST", [])
    print(f"  {len(watchlist)} symbol(s) configured")
    if len(watchlist) <= 15:
        for w in watchlist:
            sym = w.get("symbol") if isinstance(w, dict) else w
            exch = w.get("exchange", "NSE") if isinstance(w, dict) else "NSE"
            print(f"    {exch}:{sym}")
    else:
        preview = watchlist[:5]
        for w in preview:
            sym = w.get("symbol") if isinstance(w, dict) else w
            print(f"    {sym}")
        print(f"    ... and {len(watchlist) - 5} more")


def main():
    show_context()
    show_critical_flags()
    show_watchlist_summary()
    show_all_boolean_flags()
    show_mode_and_threshold_settings()
    print(f"\n{'=' * 70}")
    print("Done. Re-run any time -- this only reads config, it changes nothing.")
    print("=" * 70)


if __name__ == "__main__":
    main()
