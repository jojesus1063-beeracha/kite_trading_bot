#!/usr/bin/env python3
"""Paper-only launcher applying a 50% daily-loss ceiling.

This wrapper changes only the paper process. It preserves today's realized P&L
and trade count. If the persisted halt was caused solely by the previous daily
loss limit, it is cleared only when today's realized loss is still inside the
new 50% ceiling. Other halt reasons are never cleared here.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import config as cfg

logger = logging.getLogger("paper_50pct_risk_launcher")

PAPER_MAX_DAILY_LOSS_PCT = 50.0
DAY_STATE_PATH = Path(__file__).resolve().parent / "day_state.json"


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

    # Import only after the config override so every downstream paper component
    # sees MAX_DAILY_LOSS_PCT=50 before RiskManager is constructed.
    import paper_mae_mfe_launcher as strategy_stack

    strategy_stack.main()


if __name__ == "__main__":
    main()
