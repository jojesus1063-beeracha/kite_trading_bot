"""Apply the stewardship-risk wiring to the current main.py in-place.

Why this exists:
The live VM has orchestration files that are newer than GitHub main.
This patch therefore performs narrow, assertion-backed replacements
instead of replacing the whole file and accidentally deleting VM-only
hardening. It is safe to run once after syncing the branch to the VM.

Usage:
    python3 apply_stewardship_main_patch.py --check
    python3 apply_stewardship_main_patch.py

--check validates every expected anchor without writing anything.
"""

from __future__ import annotations

import argparse
from pathlib import Path


PATH = Path(__file__).resolve().with_name("main.py")


def replace_once(text: str, old: str, new: str, name: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{name}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def build_patched(text: str) -> str:
    import_anchor = (
        "from scheduler import candle_interval_minutes, last_completed_candle_close, "
        "next_scan_time, ScanGuard\n"
    )
    import_new = import_anchor + (
        "from stewardship_policy import (\n"
        "    entry_quality_score,\n"
        "    preserve_minimum_rr_target,\n"
        "    two_candle_adverse_confirmation,\n"
        ")\n"
    )
    if "from stewardship_policy import (" not in text:
        text = replace_once(text, import_anchor, import_new, "policy import")

    qty_anchor = """

            qty = risk.position_size(signal.entry_price, signal.stop_loss)
            if qty > 0 and not cfg.PAPER_TRADING:
                qty = cap_quantity_by_margin(kite, symbol, signal.direction, qty, exchange, cfg)
            result = place_entry_order(kite, symbol, signal.direction, qty, exchange, cfg)
"""
    qty_new = """

            quality_score = entry_quality_score(
                signal.confidence,
                signal.price_action_score or 0.0,
                signal.market_alignment,
                signal.news_confidence_score,
            )
            min_quality = float(getattr(cfg, "MIN_ENTRY_QUALITY_SCORE", 70))
            if getattr(cfg, "ENABLE_ENTRY_QUALITY_GATE", False) and quality_score < min_quality:
                reason = f"ENTRY_QUALITY_BELOW_MIN ({quality_score:.1f} < {min_quality:.1f})"
                logger.info(f"{symbol}: skipped -- {reason}")
                status_this_cycle.append({"symbol": symbol, "status": f"skipped, quality={quality_score:.1f}"})
                log_signal({
                    "timestamp": str(signal.timestamp), "symbol": symbol,
                    "market_trend": market_trend, "sector": sector_for_symbol(symbol),
                    "market_alignment": signal.market_alignment,
                    "technical_confidence": signal.confidence,
                    "entry_price": signal.entry_price, "direction": signal.direction,
                    "executed": False, "rejection_reason": reason,
                    "price_action_score": signal.price_action_score,
                    "entry_quality_score": quality_score,
                })
                continue

            qty = risk.position_size(signal.entry_price, signal.stop_loss)
            if qty > 0 and not cfg.PAPER_TRADING:
                qty = cap_quantity_by_margin(kite, symbol, signal.direction, qty, exchange, cfg)

            proposed_risk = risk.proposed_trade_risk(signal.entry_price, signal.stop_loss, qty)
            if qty <= 0 or not risk.can_take_new_trade(
                current_open_count=len(open_positions),
                open_positions=open_positions,
                proposed_risk=proposed_risk,
            ):
                total_risk = risk.total_risk_if_added(open_positions, proposed_risk)
                reason = (
                    f"TOTAL_RISK_BUDGET_REJECT proposed={proposed_risk:.2f} "
                    f"worst_case={total_risk:.2f} max={risk.max_loss_amount():.2f}"
                )
                logger.info(f"{symbol}: skipped -- {reason}")
                status_this_cycle.append({"symbol": symbol, "status": "skipped, total risk budget"})
                log_signal({
                    "timestamp": str(signal.timestamp), "symbol": symbol,
                    "market_trend": market_trend, "sector": sector_for_symbol(symbol),
                    "market_alignment": signal.market_alignment,
                    "technical_confidence": signal.confidence,
                    "entry_price": signal.entry_price, "direction": signal.direction,
                    "executed": False, "rejection_reason": reason,
                    "entry_quality_score": quality_score,
                    "proposed_stop_risk": proposed_risk,
                    "worst_case_daily_risk": total_risk,
                    "daily_risk_budget": risk.max_loss_amount(),
                })
                continue

            result = place_entry_order(kite, symbol, signal.direction, qty, exchange, cfg)
"""
    if "TOTAL_RISK_BUDGET_REJECT" not in text:
        text = replace_once(text, qty_anchor, qty_new, "quality + total risk gate")

    target_anchor = """                target_price = signal.target
                if getattr(cfg, "ENABLE_FIXED_TARGET", False):
                    try:
                        pct = getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5) / 100
                        target_price = (signal.entry_price * (1 + pct) if signal.direction == "BUY"
                                        else signal.entry_price * (1 - pct))
                    except Exception as e:
                        logger.warning(f"{symbol}: fixed target calculation failed, "
                                        f"using strategy-computed target instead: {e}")
                        target_price = signal.target
"""
    target_new = """                target_price = signal.target
                if getattr(cfg, "ENABLE_FIXED_TARGET", False):
                    try:
                        target_price = preserve_minimum_rr_target(
                            signal.direction,
                            signal.entry_price,
                            signal.target,
                            getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
                        )
                    except Exception as e:
                        logger.warning(f"{symbol}: fixed target calculation failed, "
                                        f"using strategy-computed target instead: {e}")
                        target_price = signal.target
"""
    if "preserve_minimum_rr_target(" not in text[text.find("confirmed_entry_price"):]:
        text = replace_once(text, target_anchor, target_new, "minimum R:R target")

    pos_anchor = '                    "entry_status_message": result.get("reason"),\n'
    pos_new = pos_anchor + '                    "entry_quality_score": quality_score,\n'
    if '"entry_quality_score": quality_score' not in text:
        text = replace_once(text, pos_anchor, pos_new, "persist quality score")

    flags_anchor = """    hit_trailing_stop = False
    structure_broken = False
    trend_reversed = False
    hit_target = False

"""
    flags_new = """    hit_trailing_stop = False
    structure_broken = False
    trend_reversed = False
    hit_target = False
    adverse_confirmed = False

    try:
        adverse_confirmed = two_candle_adverse_confirmation(
            df_5m,
            direction,
            pos["entry"],
            entry_time=pos.get("entry_time"),
            confirm_candles=int(getattr(cfg, "ADVERSE_EXIT_CONFIRM_CANDLES", 2)),
            last_row_is_forming=True,
            ema_period=int(getattr(cfg, "ENTRY_EMA", 20)),
        )
    except Exception as e:
        logger.warning(f"{symbol}: adverse two-candle confirmation failed: {e}")
        adverse_confirmed = False

"""
    if "adverse_confirmed = two_candle_adverse_confirmation" not in text:
        text = replace_once(text, flags_anchor, flags_new, "two-candle adverse confirmation")

    exit_if_anchor = """    if hit_hard_stop or hit_trailing_stop or structure_broken or trend_reversed or hit_target:
        if hit_hard_stop:
            result = "stop"
        elif hit_trailing_stop:
            result = "trailing_stop"
        elif structure_broken:
            result = "structure_break"
        elif hit_target:
            result = "fixed_target"
        else:
            result = "trend_reversal"
"""
    exit_if_new = """    if hit_hard_stop or adverse_confirmed or hit_trailing_stop or structure_broken or trend_reversed or hit_target:
        if hit_hard_stop:
            result = "stop"
        elif adverse_confirmed:
            result = "adverse_2candle"
        elif hit_trailing_stop:
            result = "trailing_stop"
        elif structure_broken:
            result = "structure_break"
        elif hit_target:
            result = "fixed_target"
        else:
            result = "trend_reversal"
"""
    if 'result = "adverse_2candle"' not in text:
        text = replace_once(text, exit_if_anchor, exit_if_new, "adverse exit wiring")

    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    original = PATH.read_text()
    patched = build_patched(original)

    if patched == original:
        print("main.py already contains all stewardship wiring")
        return

    if args.check:
        print("main.py anchors validated; patch can be applied cleanly")
        return

    PATH.write_text(patched)
    print("main.py stewardship wiring applied")


if __name__ == "__main__":
    main()
