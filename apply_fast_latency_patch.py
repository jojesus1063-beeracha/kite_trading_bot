"""Apply guarded live-latency hardening to config.py and main.py.

The trading VM has orchestration newer than GitHub, so this script uses
assertion-backed narrow replacements. It refuses to write if expected
anchors have drifted.

Usage:
    python3 apply_fast_latency_patch.py --check
    python3 apply_fast_latency_patch.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.py"
MAIN = ROOT / "main.py"


def replace_once(text: str, old: str, new: str, name: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{name}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_config(text: str) -> str:
    stewardship_anchor = """# Stewardship policy controls.\nADVERSE_EXIT_CONFIRM_CANDLES = 2  # require two completed adverse candles before the loss-trend exit\nENABLE_ENTRY_QUALITY_GATE = True  # consolidate existing confidence/evidence into one entry quality score\nMIN_ENTRY_QUALITY_SCORE = 70\n"""
    stewardship_new = stewardship_anchor + """\n# Fast adverse protection: do not replace the broker hard SL. This soft\n# exit acts earlier only after persistent adverse progress toward that SL.\nENABLE_FAST_ADVERSE_EXIT = True\nFAST_ADVERSE_STOP_PROGRESS = 0.60   # 60% of entry->hard-stop distance\nFAST_ADVERSE_CONFIRMATIONS = 2      # consecutive monitor checks required\n"""
    if "ENABLE_FAST_ADVERSE_EXIT" not in text:
        text = replace_once(text, stewardship_anchor, stewardship_new, "fast adverse config")

    scheduler_anchor = """ENABLE_CANDLE_ALIGNED_POLLING = False\nPOSITION_CHECK_SECONDS = 25   # how often to check open positions between scans\nSCAN_BUFFER_SECONDS = 8       # wait this long after a candle closes before scanning\n"""
    scheduler_new = """ENABLE_CANDLE_ALIGNED_POLLING = False\nPOSITION_CHECK_SECONDS = 5    # fast open-position monitoring; one position max by stewardship policy\nSCAN_BUFFER_SECONDS = 8       # wait this long after a candle closes before scanning\nSCAN_SYMBOL_THROTTLE_SECONDS = 0.35  # one throttle after both historical requests per symbol\n"""
    if "SCAN_SYMBOL_THROTTLE_SECONDS" not in text:
        text = replace_once(text, scheduler_anchor, scheduler_new, "scheduler latency defaults")

    override_anchor = """    ADVERSE_EXIT_CONFIRM_CANDLES = _overrides.get(\"adverse_exit_confirm_candles\", ADVERSE_EXIT_CONFIRM_CANDLES)\n    ENABLE_ENTRY_QUALITY_GATE = _overrides.get(\"enable_entry_quality_gate\", ENABLE_ENTRY_QUALITY_GATE)\n    MIN_ENTRY_QUALITY_SCORE = _overrides.get(\"min_entry_quality_score\", MIN_ENTRY_QUALITY_SCORE)\n"""
    override_new = override_anchor + """    ENABLE_FAST_ADVERSE_EXIT = _overrides.get(\"enable_fast_adverse_exit\", ENABLE_FAST_ADVERSE_EXIT)\n    FAST_ADVERSE_STOP_PROGRESS = _overrides.get(\"fast_adverse_stop_progress\", FAST_ADVERSE_STOP_PROGRESS)\n    FAST_ADVERSE_CONFIRMATIONS = _overrides.get(\"fast_adverse_confirmations\", FAST_ADVERSE_CONFIRMATIONS)\n    SCAN_SYMBOL_THROTTLE_SECONDS = _overrides.get(\"scan_symbol_throttle_seconds\", SCAN_SYMBOL_THROTTLE_SECONDS)\n"""
    if "fast_adverse_stop_progress" not in text:
        text = replace_once(text, override_anchor, override_new, "latency override wiring")

    return text


def patch_main(text: str) -> str:
    import_anchor = """from stewardship_policy import (\n    entry_quality_score,\n    preserve_minimum_rr_target,\n    two_candle_adverse_confirmation,\n)\n"""
    import_new = """from stewardship_policy import (\n    adverse_stop_progress,\n    entry_quality_score,\n    preserve_minimum_rr_target,\n    two_candle_adverse_confirmation,\n)\n"""
    if "    adverse_stop_progress," not in text:
        text = replace_once(text, import_anchor, import_new, "adverse progress import")

    fetch_anchor = """        df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)\n        time.sleep(0.5)\n        df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)\n        time.sleep(0.5)\n"""
    fetch_new = """        df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)\n        df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)\n        # Keep aggregate historical API pressure controlled without paying\n        # two fixed half-second sleeps for every symbol.\n        time.sleep(float(getattr(cfg, \"SCAN_SYMBOL_THROTTLE_SECONDS\", 0.35)))\n"""
    if "SCAN_SYMBOL_THROTTLE_SECONDS" not in text[text.find("def run_full_scan"):text.find("def _market_structure_broken")]:
        text = replace_once(text, fetch_anchor, fetch_new, "scan request throttle")

    stop_anchor = """    direction = pos[\"direction\"]\n    hit_hard_stop = (last_price <= pos[\"stop\"]) if direction == \"BUY\" else (last_price >= pos[\"stop\"])\n\n    hit_trailing_stop = False\n"""
    stop_new = """    direction = pos[\"direction\"]\n    hit_hard_stop = (last_price <= pos[\"stop\"]) if direction == \"BUY\" else (last_price >= pos[\"stop\"])\n\n    # Fast soft exit: react before the hard stop only when adverse movement\n    # is both materially deep into the original stop distance and persistent.\n    # The broker-side hard stop remains authoritative and immediate.\n    fast_adverse_confirmed = False\n    fast_progress = 0.0\n    if getattr(cfg, \"ENABLE_FAST_ADVERSE_EXIT\", False) and not hit_hard_stop:\n        try:\n            fast_progress = adverse_stop_progress(\n                direction,\n                pos[\"entry\"],\n                pos[\"stop\"],\n                last_price,\n            )\n            threshold = float(getattr(cfg, \"FAST_ADVERSE_STOP_PROGRESS\", 0.60))\n            required = max(1, int(getattr(cfg, \"FAST_ADVERSE_CONFIRMATIONS\", 2)))\n            previous = int(pos.get(\"fast_adverse_count\", 0) or 0)\n            if fast_progress >= threshold:\n                pos[\"fast_adverse_count\"] = previous + 1\n            else:\n                pos[\"fast_adverse_count\"] = 0\n            pos[\"fast_adverse_progress\"] = fast_progress\n            fast_adverse_confirmed = pos[\"fast_adverse_count\"] >= required\n            if pos[\"fast_adverse_count\"] != previous or fast_adverse_confirmed:\n                save_positions(open_positions)\n            if fast_adverse_confirmed:\n                logger.warning(\n                    f\"{symbol}: FAST ADVERSE EXIT confirmed | \"\n                    f\"progress={fast_progress:.2f}R toward hard stop | \"\n                    f\"checks={pos['fast_adverse_count']}/{required} | \"\n                    f\"last={last_price:.2f} entry={pos['entry']:.2f} stop={pos['stop']:.2f}\"\n                )\n        except Exception as e:\n            logger.warning(f\"{symbol}: fast adverse check failed safely: {e}\")\n            fast_adverse_confirmed = False\n\n    hit_trailing_stop = False\n"""
    if "FAST ADVERSE EXIT confirmed" not in text:
        text = replace_once(text, stop_anchor, stop_new, "fast adverse monitor")

    exit_anchor = """    if hit_hard_stop or adverse_confirmed or hit_trailing_stop or structure_broken or trend_reversed or hit_target:\n        if hit_hard_stop:\n            result = \"stop\"\n        elif adverse_confirmed:\n            result = \"adverse_2candle\"\n"""
    exit_new = """    if hit_hard_stop or fast_adverse_confirmed or adverse_confirmed or hit_trailing_stop or structure_broken or trend_reversed or hit_target:\n        if hit_hard_stop:\n            result = \"stop\"\n        elif fast_adverse_confirmed:\n            result = \"fast_adverse\"\n        elif adverse_confirmed:\n            result = \"adverse_2candle\"\n"""
    if 'result = "fast_adverse"' not in text:
        text = replace_once(text, exit_anchor, exit_new, "fast adverse exit priority")

    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    config_original = CONFIG.read_text()
    main_original = MAIN.read_text()
    config_patched = patch_config(config_original)
    main_patched = patch_main(main_original)

    if args.check:
        print("fast-latency patch anchors validated")
        return

    if config_patched != config_original:
        CONFIG.write_text(config_patched)
        print("config.py fast-latency settings applied")
    else:
        print("config.py already patched")

    if main_patched != main_original:
        MAIN.write_text(main_patched)
        print("main.py fast-latency wiring applied")
    else:
        print("main.py already patched")


if __name__ == "__main__":
    main()
