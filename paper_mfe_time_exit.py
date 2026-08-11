"""Paper-only MFE + time-in-trade exit overlay.

This module is deliberately isolated from the production/live exit engine.
It wraps main.check_position_exit only inside paper_contrarian_launcher.
Hard stop and existing fixed/hybrid target handling keep precedence.

Model:
- <10 min: no MFE/time action.
- 10-20 min: if MFE >= 0.30% and at least 50% of MFE has been given back, exit.
- 20-40 min: if MFE >= 0.50% and current profit <= 0.30%, exit to lock the move;
             otherwise if MFE >= 0.40% and at least 50% has been given back, exit.
- >40 min: if MFE >= 0.30% and at least 50% has been given back, exit.
- >60 min: if MFE never reached 0.30%, exit as a stale/non-progressing trade.

The overlay never opens a position and never runs when PAPER_TRADING is false.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger("paper_mfe_time_exit")

MIN_AGE_MINUTES = 10.0
EARLY_MFE_PCT = 0.30
MID_MFE_PCT = 0.40
LOCK_MFE_PCT = 0.50
LOCK_CURRENT_PCT = 0.30
LATE_MFE_PCT = 0.30
STALE_AFTER_MINUTES = 60.0
STALE_MAX_MFE_PCT = 0.30
GIVEBACK_FRACTION = 0.50
MAX_QUOTE_AGE_SECONDS = 60.0


def _as_float(value, default=None):
    try:
        if value is None:
            return default
        value = float(value)
        if pd.isna(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def _minutes_in_trade(position):
    entry_time = position.get("entry_time")
    if not entry_time:
        return None
    try:
        entry_dt = pd.to_datetime(entry_time)
        now = (
            pd.Timestamp.now(tz=entry_dt.tz)
            if entry_dt.tz is not None
            else pd.Timestamp.now()
        )
        return max(0.0, (now - entry_dt).total_seconds() / 60.0)
    except Exception:
        return None


def _position_status(main_module, symbol):
    """Read the most recent analytics snapshot without making a market-data call."""
    try:
        status = main_module.load_bot_status()
    except Exception:
        return None
    if not isinstance(status, dict):
        return None
    positions = status.get("positions") or []
    if isinstance(positions, dict):
        item = positions.get(symbol)
        return item if isinstance(item, dict) else None
    for item in positions:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item
    return None


def _decision(position, status):
    if not isinstance(status, dict):
        return None

    quote_age = _as_float(status.get("quote_age_seconds"), 0.0)
    if quote_age is not None and quote_age > MAX_QUOTE_AGE_SECONDS:
        return None

    minutes = _as_float(status.get("time_in_trade_minutes"))
    if minutes is None:
        minutes = _minutes_in_trade(position)
    if minutes is None or minutes < MIN_AGE_MINUTES:
        return None

    current_pct = _as_float(status.get("profit_pct"))
    if current_pct is None:
        return None

    mfe = max(
        _as_float(position.get("mfe_pct"), current_pct),
        _as_float(status.get("mfe_pct"), current_pct),
        current_pct,
    )
    mae = min(
        _as_float(position.get("mae_pct"), current_pct),
        _as_float(status.get("mae_pct"), current_pct),
        current_pct,
    )
    giveback_pct = max(0.0, mfe - current_pct)
    giveback_fraction = (giveback_pct / mfe) if mfe > 0 else 0.0

    detail = {
        "minutes": minutes,
        "current_profit_pct": current_pct,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "giveback_pct_points": giveback_pct,
        "giveback_fraction": giveback_fraction,
    }

    if minutes > STALE_AFTER_MINUTES and mfe < STALE_MAX_MFE_PCT:
        return "mfe_time_stale_60m", detail

    if minutes > 40.0:
        if mfe >= LATE_MFE_PCT and giveback_fraction >= GIVEBACK_FRACTION:
            return "mfe_time_late_giveback", detail
        return None

    if minutes >= 20.0:
        if mfe >= LOCK_MFE_PCT and current_pct <= LOCK_CURRENT_PCT:
            return "mfe_time_lock_20_40", detail
        if mfe >= MID_MFE_PCT and giveback_fraction >= GIVEBACK_FRACTION:
            return "mfe_time_giveback_20_40", detail
        return None

    # 10-20 minutes
    if mfe >= EARLY_MFE_PCT and giveback_fraction >= GIVEBACK_FRACTION:
        return "mfe_time_protect_10_20", detail
    return None


def _normal_exit_already_due(position, status):
    """Let the native hard-stop/target path win if the last snapshot already hit it."""
    if not isinstance(status, dict):
        return False
    current = _as_float(status.get("current_price"))
    stop = _as_float(position.get("stop"))
    target = _as_float(position.get("target"))
    direction = position.get("direction")
    if current is None or stop is None or target is None:
        return False
    if direction == "BUY":
        return current <= stop or current >= target
    if direction == "SELL":
        return current >= stop or current <= target
    return False


def install(main_module: Any, cfg: Any) -> None:
    """Install a paper-only wrapper around main.check_position_exit."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise RuntimeError("SAFETY BLOCK: MFE/time exit overlay requires PAPER_TRADING=True")

    original_check = main_module.check_position_exit
    original_record_trade = main_module.record_trade

    def wrapped_check(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
        position = open_positions.get(symbol)
        if position is None:
            return original_check(
                kite, symbol, tokens, exchange_map, open_positions, risk,
                check_trend=check_trend,
            )

        status = _position_status(main_module, symbol)
        decision = None if _normal_exit_already_due(position, status) else _decision(position, status)
        if decision is None:
            return original_check(
                kite, symbol, tokens, exchange_map, open_positions, risk,
                check_trend=check_trend,
            )

        reason, detail = decision
        position["mfe_time_exit_reason"] = reason
        position["mfe_time_exit_detail"] = detail
        position["mfe_time_exit_triggered_at"] = pd.Timestamp.now(tz="Asia/Kolkata").isoformat()
        main_module.save_positions(open_positions)

        logger.warning(
            "PAPER MFE-TIME EXIT TRIGGER | %s | reason=%s | age=%.1fm | current=%+.3f%% | MFE=%.3f%% | MAE=%.3f%% | giveback=%.1f%% of MFE",
            symbol,
            reason,
            detail["minutes"],
            detail["current_profit_pct"],
            detail["mfe_pct"],
            detail["mae_pct"],
            detail["giveback_fraction"] * 100.0,
        )

        # Reuse the native, already-tested exit pipeline by making its fixed-target
        # condition true for this single call. Disable hybrid splitting only for
        # this MFE/time-triggered exit so the remaining position is closed fully.
        original_target = position.get("target")
        original_hybrid_enabled = position.get("hybrid_exit_enabled")
        if position.get("direction") == "BUY":
            position["target"] = 0.000001
        else:
            position["target"] = 1.0e15
        position["hybrid_exit_enabled"] = False

        def record_trade_proxy(*args, **kwargs):
            args = list(args)
            if len(args) >= 7 and args[6] == "fixed_target":
                args[6] = reason
            elif kwargs.get("reason") == "fixed_target":
                kwargs["reason"] = reason
            analytics = dict(kwargs.get("analytics") or {})
            analytics.update({
                "mfe_time_exit_reason": reason,
                "mfe_time_exit_detail": detail,
            })
            kwargs["analytics"] = analytics
            return original_record_trade(*args, **kwargs)

        main_module.record_trade = record_trade_proxy
        try:
            result = original_check(
                kite, symbol, tokens, exchange_map, open_positions, risk,
                check_trend=check_trend,
            )
        finally:
            main_module.record_trade = original_record_trade
            if symbol in open_positions:
                remaining = open_positions[symbol]
                remaining["target"] = original_target
                remaining["hybrid_exit_enabled"] = original_hybrid_enabled
                main_module.save_positions(open_positions)

        if isinstance(result, str):
            result = result.replace("fixed_target", reason)
        return result

    main_module.check_position_exit = wrapped_check
    logger.warning(
        "PAPER MFE+TIME EXIT MODEL ACTIVE: <10m hold; 10-20m MFE>=0.30%%/50%% giveback; 20-40m MFE>=0.40%%/50%% giveback or MFE>=0.50%% falling to <=0.30%%; >40m 50%% giveback; >60m stale if MFE<0.30%%"
    )
