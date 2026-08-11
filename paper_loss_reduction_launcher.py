#!/usr/bin/env python3
"""PAPER-only loss-reduction launcher layered on the current verified policy.

Keeps the existing strategy/risk stack and adds:
- no new entries at/after 14:00 IST (NO_ENTRY_AFTER=13:59)
- block a symbol for the rest of the day after 2 consecutive completed losses
- early failure exit after >10 minutes when MFE < +0.15%, current P&L < 0,
  and 3 completed entry-timeframe candles confirm adverse EMA structure

Existing policy retained:
- ADX <20 BLOCK
- 20<=ADX<40 REVERSE EMA9/EMA21
- ADX >=40 NORMAL EMA9/EMA21
- risk/trade 0.20%, daily-loss halt 5%, emergency stop 0.75%
- existing MAE/MFE/hybrid exit stack

This launcher requires PAPER_TRADING=True and does not alter live mode.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd

import config as cfg
import paper_50pct_risk_launcher as current

logger = logging.getLogger("paper_loss_reduction_launcher")

PAPER_NO_NEW_ENTRIES_AT_OR_AFTER = "14:00"
PAPER_NO_ENTRY_AFTER = "13:59"
PAPER_CONSECUTIVE_LOSS_BLOCK = 2
PAPER_EARLY_FAILURE_MIN_AGE_MINUTES = 10.0
PAPER_EARLY_FAILURE_MAX_MFE_PCT = 0.15


def apply_loss_reduction_config() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: loss-reduction launcher requires PAPER_TRADING=True")

    # The base entry-window implementation is inclusive at NO_ENTRY_AFTER.
    # 13:59 therefore guarantees that 14:00 and later cannot create entries.
    cfg.NO_ENTRY_AFTER = PAPER_NO_ENTRY_AFTER
    cfg.PAPER_NO_NEW_ENTRIES_AT_OR_AFTER = PAPER_NO_NEW_ENTRIES_AT_OR_AFTER
    cfg.PAPER_CONSECUTIVE_LOSS_BLOCK = PAPER_CONSECUTIVE_LOSS_BLOCK
    cfg.PAPER_EARLY_FAILURE_MIN_AGE_MINUTES = PAPER_EARLY_FAILURE_MIN_AGE_MINUTES
    cfg.PAPER_EARLY_FAILURE_MAX_MFE_PCT = PAPER_EARLY_FAILURE_MAX_MFE_PCT


def _completed_symbol_groups(base, symbol, now=None, log_path=None):
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata")
    if now.tzinfo is None:
        now = now.tz_localize("Asia/Kolkata")
    else:
        now = now.tz_convert("Asia/Kolkata")
    today = now.strftime("%Y-%m-%d")
    path = Path(log_path) if log_path is not None else base.TRADE_HISTORY_PATH

    groups = {}
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("date") != today or record.get("symbol") != symbol:
                continue
            key = base._trade_group_key(record)
            group = groups.setdefault(key, {"pnl": 0.0, "last_exit": None})
            group["pnl"] += float(record.get("pnl") or 0.0)
            t = record.get("time")
            if t:
                try:
                    ts = pd.Timestamp(f"{today} {t}", tz="Asia/Kolkata")
                    if group["last_exit"] is None or ts > group["last_exit"]:
                        group["last_exit"] = ts
                except Exception:
                    pass

    return sorted(
        (g for g in groups.values() if g.get("last_exit") is not None),
        key=lambda g: g["last_exit"],
    )


def install_consecutive_loss_guard() -> None:
    import paper_contrarian_launcher as base

    def guard(symbol, now=None, log_path=None):
        try:
            completed = _completed_symbol_groups(base, symbol, now=now, log_path=log_path)
        except OSError as exc:
            return True, {
                "paper_only": True,
                "active": True,
                "decision": "ALLOW_FAIL_OPEN",
                "history_read_error": str(exc),
            }

        trailing_losses = 0
        for group in reversed(completed):
            if float(group.get("pnl") or 0.0) < 0:
                trailing_losses += 1
            else:
                break

        blocked = trailing_losses >= PAPER_CONSECUTIVE_LOSS_BLOCK
        detail = {
            "paper_only": True,
            "active": True,
            "decision": "BLOCK" if blocked else "ALLOW",
            "reason": "CONSECUTIVE_SYMBOL_LOSSES" if blocked else "OK",
            "consecutive_completed_losses": trailing_losses,
            "block_at": PAPER_CONSECUTIVE_LOSS_BLOCK,
        }
        return (not blocked), detail

    base._paper_entry_guard = guard
    logger.warning(
        "PAPER SYMBOL LOSS GUARD ACTIVE: block symbol for rest of day after %s consecutive completed losses",
        PAPER_CONSECUTIVE_LOSS_BLOCK,
    )


def install_early_failure_exit_patch(trading_main, mae, mfe_time) -> None:
    """Exit weak trades after 10m when they never develop and structure is adverse."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: early-failure exit requires PAPER_TRADING=True")

    original_check = trading_main.check_position_exit

    def check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
        # Existing native -> MAE -> MFE/time stack retains priority.
        status = original_check(
            kite, symbol, tokens, exchange_map, open_positions, risk,
            check_trend=check_trend,
        )
        if symbol not in open_positions or not str(status).lower().startswith("position open"):
            return status

        position = open_positions[symbol]
        minutes = mae._minutes_in_trade(position)
        if minutes is None or minutes <= PAPER_EARLY_FAILURE_MIN_AGE_MINUTES:
            return status

        token = tokens.get(symbol)
        if token is None:
            return status
        exchange = position.get("exchange", exchange_map.get(symbol, "NSE"))

        try:
            df = trading_main.fetch_candles(
                kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=1, trim_incomplete=False
            )
            time.sleep(0.35)
            if df is None or df.empty:
                return status
            completed = mae._completed_entry_candles(df, trading_main)
            if completed.empty:
                return status

            last_price = float(df.iloc[-1]["close"])
            entry_dt = pd.to_datetime(position.get("entry_time")) if position.get("entry_time") else None
            df_since = df
            if entry_dt is not None and "date" in df.columns:
                dates = pd.to_datetime(df["date"])
                candidate = df.loc[dates >= entry_dt]
                if not candidate.empty:
                    df_since = candidate

            mfe, adverse_excursion, current_pct, giveback_pct = mfe_time._excursions(
                position, df_since, last_price
            )
            adverse, adverse_detail = mae._adverse_trend(position, completed, trading_main)
        except Exception as exc:
            logger.warning("%s: early-failure observation failed: %s", symbol, exc)
            return status

        trigger = (
            mfe < PAPER_EARLY_FAILURE_MAX_MFE_PCT
            and current_pct < 0.0
            and adverse
        )
        if not trigger:
            return status

        reason = "early_failure_low_mfe_10m"
        qty = int(position.get("qty") or 0)
        if qty <= 0:
            return status

        exit_result = trading_main.place_exit_order(
            kite, symbol, position["direction"], qty, exchange, cfg,
            protection_clearance=None,
        )
        confirmed_qty = int(exit_result.get("filled_quantity") or 0)
        if confirmed_qty <= 0:
            return f"EXIT NOT FILLED ({reason})"

        exit_price = float(exit_result.get("average_price") or last_price)
        cost_result = trading_main.net_pnl_for_trade(
            position["direction"], confirmed_qty, float(position["entry"]), exit_price
        )
        net_pnl = float(cost_result["net_pnl"])
        risk.record_trade_result(net_pnl)

        analytics = trading_main._trade_analytics_from_position(position)
        analytics.update({
            "early_failure_rule": reason,
            "early_failure_minutes": minutes,
            "early_failure_mfe_pct": mfe,
            "early_failure_mae_pct": adverse_excursion,
            "early_failure_current_pct": current_pct,
            "early_failure_adverse_detail": adverse_detail,
        })
        trading_main.record_trade(
            symbol, position["direction"], confirmed_qty, float(position["entry"]),
            exit_price, net_pnl, reason, exchange=exchange,
            gross_pnl=cost_result["gross_pnl"], costs=cost_result["costs"],
            analytics=analytics,
        )

        remaining = qty - confirmed_qty
        if remaining <= 0:
            del open_positions[symbol]
        else:
            position["qty"] = remaining
        trading_main.save_positions(open_positions)

        logger.warning(
            "%s: EARLY FAILURE EXIT | time=%.1fm MFE=%.3f%% current=%.3f%% adverse=%s net=%.2f",
            symbol, minutes, mfe, current_pct, adverse, net_pnl,
        )
        return f"CLOSED ({reason}) | net P&L {net_pnl:.2f}"

    trading_main.check_position_exit = check_position_exit
    logger.warning(
        "PAPER EARLY FAILURE EXIT ACTIVE: >%.0fm AND MFE<%.2f%% AND current<0 AND 3 adverse EMA candles",
        PAPER_EARLY_FAILURE_MIN_AGE_MINUTES,
        PAPER_EARLY_FAILURE_MAX_MFE_PCT,
    )


def main() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: loss-reduction launcher requires PAPER_TRADING=True")

    current.apply_paper_risk_overrides()
    apply_loss_reduction_config()
    current.install_paper_emergency_stop_override()
    current.install_direction_only_adx_policy()
    install_consecutive_loss_guard()

    import paper_contrarian_launcher as base
    import paper_mae_mfe_launcher as mae
    import paper_mfe_time_launcher as mfe_time

    base.install_two_indicator_patch()
    import main as trading_main

    mae.install_mae_adverse_exit_patch(trading_main)
    mfe_time.install_mfe_time_exit_patch(trading_main)
    install_early_failure_exit_patch(trading_main, mae, mfe_time)

    logger.warning(
        "PAPER LOSS-REDUCTION STACK ACTIVE: ADX gate/regime + <14:00 entries + 2-loss symbol block + early low-MFE exit"
    )
    trading_main.run()


if __name__ == "__main__":
    main()
