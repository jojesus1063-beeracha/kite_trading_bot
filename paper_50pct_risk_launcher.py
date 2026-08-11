#!/usr/bin/env python3
"""Paper-only launcher applying experimental paper risk overrides.

This wrapper changes only the paper process. It preserves today's realized P&L
and trade count. If the persisted halt was caused solely by the previous daily
loss limit, it is cleared only when today's realized loss is still inside the
new 50% ceiling. Other halt reasons are never cleared here.

Paper-only hard-stop experiment:
- Position sizing still uses the strategy-computed stop distance.
- Hybrid 1R/2R targets are configured from the real/original stop first.
- After the confirmed paper position is fully built, the initial hard-loss stop
  is replaced with a non-triggering sentinel.
- The original stop is retained as paper_original_stop for audit/reporting.
- If a hybrid scalp later succeeds and moves the runner stop to breakeven, that
  breakeven protection remains active; only the initial hard-loss stop is off.
- Live trading is never modified by this launcher.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import config as cfg

logger = logging.getLogger("paper_50pct_risk_launcher")

PAPER_MAX_DAILY_LOSS_PCT = 50.0
PAPER_DISABLE_INITIAL_HARD_STOP = True
DAY_STATE_PATH = Path(__file__).resolve().parent / "day_state.json"

# Finite JSON-safe values that cannot realistically be crossed by NSE equity
# prices. They are applied only after risk sizing + hybrid target construction.
_PAPER_BUY_DISABLED_STOP = -1.0
_PAPER_SELL_DISABLED_STOP = 1_000_000_000_000.0


def _paper_disable_initial_hard_stop(position: dict) -> dict:
    """Mask only a newly built PAPER position's initial loss stop.

    Do not re-mask a position that already carries the marker. This is
    deliberate: after a successful hybrid scalp, hybrid_exit may move the
    runner stop to breakeven. That breakeven protection should survive.
    """

    if not PAPER_DISABLE_INITIAL_HARD_STOP:
        return position

    if not isinstance(position, dict):
        return position

    if position.get("paper_initial_hard_stop_disabled"):
        return position

    direction = str(position.get("direction") or "").upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError(
            "Cannot disable paper hard stop without BUY/SELL direction"
        )

    original_stop = position.get("stop")
    if original_stop is not None:
        position["paper_original_stop"] = float(original_stop)

    position["paper_initial_hard_stop_disabled"] = True
    position["stop"] = (
        _PAPER_BUY_DISABLED_STOP
        if direction == "BUY"
        else _PAPER_SELL_DISABLED_STOP
    )
    return position


def install_paper_hard_stop_override() -> None:
    """Patch position construction before main.py imports the builders."""

    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper hard-stop override requires PAPER_TRADING=True"
        )

    if not PAPER_DISABLE_INITIAL_HARD_STOP:
        return

    import entry_protection

    original_build_confirmed = entry_protection.build_confirmed_position
    original_build_recovered = entry_protection.build_recovered_position

    if not getattr(original_build_confirmed, "_paper_hard_stop_wrapped", False):
        def build_confirmed_position(*args, **kwargs):
            position = original_build_confirmed(*args, **kwargs)
            return _paper_disable_initial_hard_stop(position)

        build_confirmed_position._paper_hard_stop_wrapped = True
        entry_protection.build_confirmed_position = build_confirmed_position

    if not getattr(original_build_recovered, "_paper_hard_stop_wrapped", False):
        def build_recovered_position(*args, **kwargs):
            position = original_build_recovered(*args, **kwargs)
            return _paper_disable_initial_hard_stop(position)

        build_recovered_position._paper_hard_stop_wrapped = True
        entry_protection.build_recovered_position = build_recovered_position

    logger.warning(
        "PAPER INITIAL HARD STOP DISABLED: original stop retained for audit/hybrid; "
        "native initial loss-stop trigger masked; hybrid breakeven protection remains enabled"
    )


def apply_paper_daily_loss_override() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper 50% daily-loss launcher requires PAPER_TRADING=True"
        )

    cfg.MAX_DAILY_LOSS_PCT = PAPER_MAX_DAILY_LOSS_PCT
    capital = float(getattr(cfg, "CAPITAL", 0.0) or 0.0)
    max_loss_amount = capital * PAPER_MAX_DAILY_LOSS_PCT / 100.0

    cleared = False
    realized_pnl = None
    trades_taken = None

    if DAY_STATE_PATH.exists():
        try:
            state = json.loads(DAY_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read paper day state for halt re-evaluation: %s", exc)
            state = None

        if isinstance(state, dict) and state.get("date") == date.today().isoformat():
            realized_pnl = float(state.get("realized_pnl", 0.0) or 0.0)
            trades_taken = int(state.get("trades_taken", 0) or 0)
            halt_reason = str(state.get("halt_reason", "") or "")
            daily_loss_halt = bool(state.get("halted")) and halt_reason.startswith(
                "Daily loss limit"
            )

            # RiskManager halts when realized_pnl <= -max_loss_amount, so only
            # release the old daily-loss halt when we are strictly inside the
            # new 50% paper ceiling.
            if daily_loss_halt and realized_pnl > -max_loss_amount:
                state["halted"] = False
                state["halt_reason"] = ""
                DAY_STATE_PATH.write_text(
                    json.dumps(state, indent=2) + "\n",
                    encoding="utf-8",
                )
                cleared = True

    logger.warning(
        "PAPER DAILY LOSS LIMIT ACTIVE: %.1f%% | capital=%.2f | max_daily_loss=%.2f | "
        "today_realized_pnl=%s | trades_taken=%s | previous_daily_loss_halt_cleared=%s",
        PAPER_MAX_DAILY_LOSS_PCT,
        capital,
        max_loss_amount,
        realized_pnl,
        trades_taken,
        cleared,
    )


def main() -> None:
    apply_paper_daily_loss_override()

    # Install before importing the downstream strategy stack. main.py imports
    # build_confirmed_position/build_recovered_position by name, so doing this
    # first keeps the modification strictly inside this paper process.
    install_paper_hard_stop_override()

    # Import only after the config/position-construction overrides so every
    # downstream paper component sees the intended paper-only behavior before
    # RiskManager and main.py are loaded.
    import paper_mae_mfe_launcher as strategy_stack

    strategy_stack.main()


if __name__ == "__main__":
    main()
