#!/usr/bin/env python3
"""PAPER-only launcher for the current loss-control experiment.

This wrapper changes only the PAPER process. Live configuration and broker
protection remain untouched.

Current PAPER policy:
- risk per trade: 0.20%
- ADX <20: BLOCK entry
- 20 <= ADX <40: REVERSED EMA9/EMA21 direction
- ADX >=40: NORMAL EMA9/EMA21 direction
- no ADX entry rejection at or above 20
- no paper daily entry-count cap
- no paper per-symbol completed-entry cap
- no post-loss cooldown
- no practical max-open-position cap for distinct symbols
- same-symbol concurrent entries remain blocked by the core position model,
  because open_positions is keyed by symbol and cannot safely represent two
  independent same-symbol positions
- daily realized-loss halt: 5.0%
- executable PAPER emergency stop: 0.75%
- MAE/MFE settings are passed to the downstream paper exit wrappers

The strategy's 0.45% stop geometry is retained for position sizing and hybrid
1R/2R target construction; after position construction, the PAPER executable
stop is widened to the 0.75% emergency stop. A successful hybrid scalp may
still move the runner stop to breakeven.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import config as cfg

logger = logging.getLogger("paper_50pct_risk_launcher")

PAPER_RISK_PER_TRADE_PCT = 0.20
PAPER_MAX_ENTRIES_PER_DAY = 10_000
PAPER_CORE_MAX_TRADES_PER_DAY = 10_000
PAPER_MAX_OPEN_POSITIONS = 10_000
PAPER_MAX_DAILY_LOSS_PCT = 5.0
PAPER_EMERGENCY_STOP_PCT = 0.75
PAPER_MAX_TRADES_PER_SYMBOL = 0
PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = 0.0
PAPER_ADX_BLOCK_LOW = 0.0
PAPER_ADX_BLOCK_HIGH = 20.0
PAPER_ADX_REVERSE_FROM = 20.0
PAPER_ADX_HIGH_NORMAL_FROM = 40.0
# Backward-compatible audit/config name: NORMAL begins at the high zone.
PAPER_ADX_NORMAL_FROM = PAPER_ADX_HIGH_NORMAL_FROM

PAPER_MAE_MIN_AGE_MINUTES = 10.0
PAPER_MAE_THRESHOLD_PCT = -0.30
PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT = -0.15
PAPER_MAE_MAX_MFE_FAILURE_PCT = 0.30
PAPER_MAE_ADVERSE_CANDLES_REQUIRED = 3
PAPER_MFE_MIN_HOLD_MINUTES = 20.0
PAPER_MFE_MID_END_MINUTES = 40.0
PAPER_MFE_MID_THRESHOLD_PCT = 0.40
PAPER_MFE_LOCK_THRESHOLD_PCT = 0.50
PAPER_MFE_LOCK_CURRENT_PCT = 0.30
PAPER_MFE_LATE_THRESHOLD_PCT = 0.30
PAPER_MFE_GIVEBACK_PCT = 50.0
PAPER_DEAD_TRADE_MINUTES = 40.0
PAPER_DEAD_TRADE_MAX_MFE_PCT = 0.30

DAY_STATE_PATH = Path(__file__).resolve().parent / "day_state.json"


def apply_paper_risk_overrides() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper risk launcher requires PAPER_TRADING=True"
        )

    cfg.RISK_PER_TRADE_PCT = PAPER_RISK_PER_TRADE_PCT
    cfg.MAX_TRADES_PER_DAY = PAPER_CORE_MAX_TRADES_PER_DAY
    cfg.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
    cfg.MAX_DAILY_LOSS_PCT = PAPER_MAX_DAILY_LOSS_PCT

    cfg.PAPER_MAX_ENTRIES_PER_DAY = PAPER_MAX_ENTRIES_PER_DAY
    cfg.PAPER_MAX_TRADES_PER_SYMBOL = PAPER_MAX_TRADES_PER_SYMBOL
    cfg.PAPER_LOSS_REENTRY_COOLDOWN_MINUTES = PAPER_LOSS_REENTRY_COOLDOWN_MINUTES
    cfg.PAPER_ADX_BLOCK_LOW = PAPER_ADX_BLOCK_LOW
    cfg.PAPER_ADX_BLOCK_HIGH = PAPER_ADX_BLOCK_HIGH
    cfg.PAPER_ADX_REVERSE_FROM = PAPER_ADX_REVERSE_FROM
    cfg.PAPER_ADX_HIGH_NORMAL_FROM = PAPER_ADX_HIGH_NORMAL_FROM
    cfg.PAPER_ADX_NORMAL_FROM = PAPER_ADX_NORMAL_FROM
    cfg.PAPER_EMERGENCY_STOP_PCT = PAPER_EMERGENCY_STOP_PCT

    cfg.PAPER_MAE_MIN_AGE_MINUTES = PAPER_MAE_MIN_AGE_MINUTES
    cfg.PAPER_MAE_THRESHOLD_PCT = PAPER_MAE_THRESHOLD_PCT
    cfg.PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT = PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT
    cfg.PAPER_MAE_MAX_MFE_FAILURE_PCT = PAPER_MAE_MAX_MFE_FAILURE_PCT
    cfg.PAPER_MAE_ADVERSE_CANDLES_REQUIRED = PAPER_MAE_ADVERSE_CANDLES_REQUIRED
    cfg.PAPER_MFE_MIN_HOLD_MINUTES = PAPER_MFE_MIN_HOLD_MINUTES
    cfg.PAPER_MFE_MID_END_MINUTES = PAPER_MFE_MID_END_MINUTES
    cfg.PAPER_MFE_MID_THRESHOLD_PCT = PAPER_MFE_MID_THRESHOLD_PCT
    cfg.PAPER_MFE_LOCK_THRESHOLD_PCT = PAPER_MFE_LOCK_THRESHOLD_PCT
    cfg.PAPER_MFE_LOCK_CURRENT_PCT = PAPER_MFE_LOCK_CURRENT_PCT
    cfg.PAPER_MFE_LATE_THRESHOLD_PCT = PAPER_MFE_LATE_THRESHOLD_PCT
    cfg.PAPER_MFE_GIVEBACK_PCT = PAPER_MFE_GIVEBACK_PCT
    cfg.PAPER_DEAD_TRADE_MINUTES = PAPER_DEAD_TRADE_MINUTES
    cfg.PAPER_DEAD_TRADE_MAX_MFE_PCT = PAPER_DEAD_TRADE_MAX_MFE_PCT

    capital = float(getattr(cfg, "CAPITAL", 0.0) or 0.0)
    max_loss_amount = capital * PAPER_MAX_DAILY_LOSS_PCT / 100.0

    cleared = False
    realized_pnl = None
    trades_taken = None
    if DAY_STATE_PATH.exists():
        try:
            state = json.loads(DAY_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read paper day state: %s", exc)
            state = None

        if isinstance(state, dict) and state.get("date") == date.today().isoformat():
            realized_pnl = float(state.get("realized_pnl", 0.0) or 0.0)
            trades_taken = int(state.get("trades_taken", 0) or 0)
            halt_reason = str(state.get("halt_reason", "") or "")
            daily_loss_halt = bool(state.get("halted")) and halt_reason.startswith(
                "Daily loss limit"
            )
            if daily_loss_halt and realized_pnl > -max_loss_amount:
                state["halted"] = False
                state["halt_reason"] = ""
                DAY_STATE_PATH.write_text(
                    json.dumps(state, indent=2) + "\n",
                    encoding="utf-8",
                )
                cleared = True

    logger.warning(
        "PAPER POLICY ACTIVE: risk/trade=%.2f%% ADX=<20 BLOCK, 20<=ADX<40 REVERSE, >=40 NORMAL, "
        "entry cap=OFF, per-symbol completed-entry cap=OFF, loss cooldown=OFF, "
        "max distinct open=%s, daily_loss=%.1f%% (Rs %.2f), emergency_stop=%.2f%% | "
        "today_pnl=%s core_exit_fill_count=%s previous_daily_loss_halt_cleared=%s",
        PAPER_RISK_PER_TRADE_PCT,
        PAPER_MAX_OPEN_POSITIONS,
        PAPER_MAX_DAILY_LOSS_PCT,
        max_loss_amount,
        PAPER_EMERGENCY_STOP_PCT,
        realized_pnl,
        trades_taken,
        cleared,
    )


def _paper_apply_emergency_stop(position: dict) -> dict:
    if not isinstance(position, dict):
        return position
    if position.get("paper_emergency_stop_active"):
        return position

    direction = str(position.get("direction") or "").upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("Cannot apply paper emergency stop without BUY/SELL direction")

    entry = float(position.get("entry") or 0.0)
    if entry <= 0:
        raise ValueError("Cannot apply paper emergency stop without a valid entry price")

    strategy_stop = position.get("stop")
    if strategy_stop is not None:
        position["paper_strategy_stop"] = float(strategy_stop)
        position["paper_original_stop"] = float(strategy_stop)

    pct = float(getattr(cfg, "PAPER_EMERGENCY_STOP_PCT", PAPER_EMERGENCY_STOP_PCT))
    fraction = pct / 100.0
    emergency_stop = (
        entry * (1.0 - fraction)
        if direction == "BUY"
        else entry * (1.0 + fraction)
    )

    position["paper_initial_hard_stop_disabled"] = False
    position["paper_emergency_stop_active"] = True
    position["paper_emergency_stop_pct"] = pct
    position["paper_emergency_stop"] = emergency_stop
    position["stop"] = emergency_stop
    return position


def install_paper_emergency_stop_override() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit(
            "SAFETY BLOCK: paper emergency-stop override requires PAPER_TRADING=True"
        )

    import entry_protection

    original_build_confirmed = entry_protection.build_confirmed_position
    original_build_recovered = entry_protection.build_recovered_position

    if not getattr(original_build_confirmed, "_paper_emergency_stop_wrapped", False):
        def build_confirmed_position(*args, **kwargs):
            position = original_build_confirmed(*args, **kwargs)
            return _paper_apply_emergency_stop(position)

        build_confirmed_position._paper_emergency_stop_wrapped = True
        entry_protection.build_confirmed_position = build_confirmed_position

    if not getattr(original_build_recovered, "_paper_emergency_stop_wrapped", False):
        def build_recovered_position(*args, **kwargs):
            position = original_build_recovered(*args, **kwargs)
            return _paper_apply_emergency_stop(position)

        build_recovered_position._paper_emergency_stop_wrapped = True
        entry_protection.build_recovered_position = build_recovered_position

    logger.warning(
        "PAPER EMERGENCY STOP ACTIVE: %.2f%% from confirmed entry; strategy stop retained for hybrid geometry",
        PAPER_EMERGENCY_STOP_PCT,
    )


def install_direction_only_adx_policy() -> None:
    """Install the requested ADX gate/regime while leaving frequency guards off."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: ADX paper policy requires PAPER_TRADING=True")

    import paper_contrarian_launcher as base

    def adx_entry_blocked(adx):
        if adx is None:
            return False
        try:
            return float(adx) < PAPER_ADX_REVERSE_FROM
        except (TypeError, ValueError):
            return False

    base._adx_entry_blocked = adx_entry_blocked

    def allow_symbol_guard(symbol, now=None, log_path=None):
        return True, {
            "paper_only": True,
            "active": False,
            "decision": "ALLOW",
            "reason": "PAPER_FREQUENCY_GUARDS_DISABLED",
        }

    base._paper_entry_guard = allow_symbol_guard

    def regime_ema_direction(df, adx=None):
        if df is None or df.empty or "close" not in df.columns or len(df) < base.EMA_SLOW:
            return None, None, None

        close = base.pd.to_numeric(df["close"], errors="coerce")
        e9 = close.ewm(span=base.EMA_FAST, adjust=False).mean().iloc[-1]
        e21 = close.ewm(span=base.EMA_SLOW, adjust=False).mean().iloc[-1]
        if base.pd.isna(e9) or base.pd.isna(e21):
            return None, None, None

        # ADX below 20 is rejected before this point. Missing ADX retains the
        # previous fail-safe reversed behavior. 20<=ADX<40 is reversed and
        # ADX>=40 follows normal EMA direction.
        normal = adx is not None and float(adx) >= PAPER_ADX_HIGH_NORMAL_FROM

        if e9 > e21:
            direction = "BUY" if normal else "SELL"
        elif e9 < e21:
            direction = "SELL" if normal else "BUY"
        else:
            direction = None
        return direction, float(e9), float(e21)

    base.ema_direction = regime_ema_direction
    # Keep the base evaluator's displayed single-threshold regime aligned with
    # the high-NORMAL boundary; exact ADX=40 must be NORMAL.
    base.ADX_REGIME_THRESHOLD = PAPER_ADX_HIGH_NORMAL_FROM - 1e-9

    original_append = base._append
    if not getattr(original_append, "_adx_block_reverse_normal_sanitizer", False):
        def append_policy(payload):
            if isinstance(payload, dict) and payload.get("event") == "ENTRY_EVALUATION":
                adx = payload.get("adx")
                if adx is None:
                    regime = "REVERSED_ADX_UNAVAILABLE"
                    blocked = False
                else:
                    value = float(adx)
                    blocked = value < PAPER_ADX_REVERSE_FROM
                    if blocked:
                        regime = "BLOCKED_WEAK_ADX"
                    elif value < PAPER_ADX_HIGH_NORMAL_FROM:
                        regime = "REVERSED"
                    else:
                        regime = "NORMAL"

                payload["adx_regime"] = regime
                payload["adx_blocks_trade"] = blocked
                policy = payload.setdefault("directional_policy", {})
                policy.clear()
                policy.update({
                    "adx_lt_20": "BLOCK_ENTRY",
                    "adx_20_to_lt_40": "REVERSED_EMA",
                    "adx_gte_40": "NORMAL_EMA",
                    "adx_unavailable": "REVERSED_EMA_FAILSAFE",
                    "normal_ema9_gt_ema21": "BUY",
                    "normal_ema9_lt_ema21": "SELL",
                    "reversed_ema9_gt_ema21": "SELL",
                    "reversed_ema9_lt_ema21": "BUY",
                    "rsi_gte_70": "BUY_OVERRIDE_AFTER_ADX_GATE",
                    "rsi_lte_30": "SELL_OVERRIDE_AFTER_ADX_GATE",
                    "rsi_30_70": "PASS_ADX_EMA_DIRECTION",
                })
            original_append(payload)

        append_policy._adx_block_reverse_normal_sanitizer = True
        base._append = append_policy

    original_snapshot = base._config_snapshot
    if not getattr(original_snapshot, "_adx_block_reverse_normal_snapshot", False):
        def snapshot_policy():
            original_snapshot()
            try:
                data = json.loads(base.CONFIG_AUDIT.read_text(encoding="utf-8"))
                data.update({
                    "PAPER_ADX_ROLE": "ENTRY_GATE_PLUS_DIRECTION_REGIME",
                    "PAPER_ADX_POLICY": "ADX<20_BLOCK__20<=ADX<40_REVERSE__ADX>=40_NORMAL",
                    "PAPER_ADX_BLOCK_BELOW": PAPER_ADX_REVERSE_FROM,
                    "PAPER_ADX_REVERSE_FROM": PAPER_ADX_REVERSE_FROM,
                    "PAPER_ADX_HIGH_NORMAL_FROM": PAPER_ADX_HIGH_NORMAL_FROM,
                    "PAPER_ENTRY_CAP": "DISABLED",
                    "PAPER_PER_SYMBOL_COMPLETED_ENTRY_CAP": "DISABLED",
                    "PAPER_LOSS_REENTRY_COOLDOWN": "DISABLED",
                })
                base.CONFIG_AUDIT.write_text(
                    json.dumps(data, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Could not augment paper config audit: %s", exc)

        snapshot_policy._adx_block_reverse_normal_snapshot = True
        base._config_snapshot = snapshot_policy

    logger.warning(
        "PAPER ADX POLICY ACTIVE: ADX<20 BLOCK; 20<=ADX<40 REVERSE; ADX>=40 NORMAL"
    )


def main() -> None:
    apply_paper_risk_overrides()
    install_paper_emergency_stop_override()
    install_direction_only_adx_policy()

    # Do NOT install paper_entry_guard: daily/per-symbol/cooldown entry blockers
    # are intentionally disabled for this experiment.
    import paper_mae_mfe_launcher as strategy_stack
    strategy_stack.main()


if __name__ == "__main__":
    main()
