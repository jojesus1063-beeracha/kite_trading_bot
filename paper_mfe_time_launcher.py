#!/usr/bin/env python3
"""Paper-only launcher adding the selected MFE + time exit management model.

This wraps the existing paper ADX/EMA/RSI launcher without changing live logic.
Emergency stop, fixed/hybrid targets and existing position exits run first. Only
when an existing position remains open does this paper-only layer consider an
MFE/time exit.

Selected rules:
- <20 min: no MFE/time exit; give the trade room.
- 20-40 min:
    * if MFE >= 0.50% and current profit has fallen to <=0.30%, close to lock it;
    * otherwise if MFE >= 0.40% and >=50% is given back, close it.
- >=40 min dead-trade protection:
    * if MFE < 0.30% and current P&L is negative, close the position.
- >40 min profit giveback:
    * if MFE >= 0.30% and >=50% is given back, close it.

Still disabled:
- 10-20 minute MFE protection exit.
- old >60 minute stale-only exit.

MFE/time never blocks an entry and never changes live trading.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

import config as cfg
import paper_contrarian_launcher as base

logger = logging.getLogger("paper_mfe_time_launcher")

MIN_HOLD_MINUTES = float(getattr(cfg, "PAPER_MFE_MIN_HOLD_MINUTES", 20.0))
MID_END_MINUTES = float(getattr(cfg, "PAPER_MFE_MID_END_MINUTES", 40.0))
MID_MFE_PCT = float(getattr(cfg, "PAPER_MFE_MID_THRESHOLD_PCT", 0.40))
LOCK_MFE_PCT = float(getattr(cfg, "PAPER_MFE_LOCK_THRESHOLD_PCT", 0.50))
LOCK_CURRENT_PCT = float(getattr(cfg, "PAPER_MFE_LOCK_CURRENT_PCT", 0.30))
LATE_MFE_PCT = float(getattr(cfg, "PAPER_MFE_LATE_THRESHOLD_PCT", 0.30))
GIVEBACK_PCT = float(getattr(cfg, "PAPER_MFE_GIVEBACK_PCT", 50.0))
DEAD_TRADE_MINUTES = float(getattr(cfg, "PAPER_DEAD_TRADE_MINUTES", 40.0))
DEAD_TRADE_MAX_MFE_PCT = float(getattr(cfg, "PAPER_DEAD_TRADE_MAX_MFE_PCT", 0.30))


def _minutes_in_trade(position) -> float | None:
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


def _excursions(position, df, last_price):
    entry = float(position["entry"])
    direction = position["direction"]

    if df is None or df.empty:
        highs = pd.Series([last_price], dtype=float)
        lows = pd.Series([last_price], dtype=float)
    else:
        highs = pd.to_numeric(df["high"], errors="coerce").dropna()
        lows = pd.to_numeric(df["low"], errors="coerce").dropna()
        if highs.empty:
            highs = pd.Series([last_price], dtype=float)
        if lows.empty:
            lows = pd.Series([last_price], dtype=float)

    if direction == "BUY":
        mfe = (float(highs.max()) - entry) / entry * 100.0
        mae = (float(lows.min()) - entry) / entry * 100.0
        current_pct = (float(last_price) - entry) / entry * 100.0
    else:
        mfe = (entry - float(lows.min())) / entry * 100.0
        mae = (entry - float(highs.max())) / entry * 100.0
        current_pct = (entry - float(last_price)) / entry * 100.0

    mfe = max(0.0, mfe)
    mae = min(0.0, mae)
    giveback_pct = 0.0 if mfe <= 0 else max(0.0, (mfe - current_pct) / mfe * 100.0)
    return mfe, mae, current_pct, giveback_pct


def _mfe_time_reason(minutes, mfe, current_pct, giveback_pct):
    if minutes is None or minutes < MIN_HOLD_MINUTES:
        return None

    # Dead trade: after 40 minutes it has never demonstrated the 0.30% MFE
    # required by the late profit-protection regime and is currently losing.
    if (
        minutes >= DEAD_TRADE_MINUTES
        and mfe < DEAD_TRADE_MAX_MFE_PCT
        and current_pct < 0.0
    ):
        return "mfe_time_dead_loser_40m"

    # >40 minutes: retain the late-giveback profit-protection rule.
    if minutes > MID_END_MINUTES:
        if mfe >= LATE_MFE_PCT and giveback_pct >= GIVEBACK_PCT:
            return "mfe_time_late_giveback"
        return None

    # 20-40 minutes: retain the profitable lock/giveback rules.
    if mfe >= LOCK_MFE_PCT and current_pct <= LOCK_CURRENT_PCT:
        return "mfe_time_lock_20_40"
    if mfe >= MID_MFE_PCT and giveback_pct >= GIVEBACK_PCT:
        return "mfe_time_giveback_20_40"
    return None


def install_mfe_time_exit_patch(trading_main):
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: MFE/time exit launcher requires PAPER_TRADING=True")

    original_check = trading_main.check_position_exit

    def check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
        # Existing emergency stop / target / hybrid exits always get first priority.
        status = original_check(
            kite,
            symbol,
            tokens,
            exchange_map,
            open_positions,
            risk,
            check_trend=check_trend,
        )

        if symbol not in open_positions:
            return status
        if not str(status).lower().startswith("position open"):
            return status

        position = open_positions[symbol]
        minutes = _minutes_in_trade(position)
        if minutes is None or minutes < MIN_HOLD_MINUTES:
            return status

        exchange = position.get("exchange", exchange_map.get(symbol, "NSE"))
        token = tokens.get(symbol)
        if token is None:
            return status

        try:
            df = trading_main.fetch_candles(
                kite,
                token,
                cfg.ENTRY_TIMEFRAME,
                lookback_days=1,
                trim_incomplete=False,
            )
            # Keep the extra paper-only historical request paced so this
            # analytics/exit overlay does not burst the broker API.
            time.sleep(0.35)
            if df is None or df.empty:
                return status

            entry_dt = pd.to_datetime(position.get("entry_time")) if position.get("entry_time") else None
            if entry_dt is not None and "date" in df.columns:
                dates = pd.to_datetime(df["date"])
                try:
                    df_since = df.loc[dates >= entry_dt]
                    if not df_since.empty:
                        df = df_since
                except Exception:
                    pass

            last_price = float(df.iloc[-1]["close"])
            mfe, mae, current_pct, giveback_pct = _excursions(position, df, last_price)
        except Exception as exc:
            logger.warning("%s: MFE/time observation failed: %s", symbol, exc)
            return status

        position["mfe_pct"] = mfe
        position["mae_pct"] = mae
        position["mfe_time_current_pct"] = current_pct
        position["mfe_time_giveback_pct"] = giveback_pct
        position["mfe_time_minutes"] = minutes
        trading_main.save_positions(open_positions)

        reason = _mfe_time_reason(minutes, mfe, current_pct, giveback_pct)
        if reason is None:
            logger.info(
                "%s: MFE/TIME HOLD | minutes=%.1f mfe=%.3f%% mae=%.3f%% current=%.3f%% giveback=%.1f%%",
                symbol, minutes, mfe, mae, current_pct, giveback_pct,
            )
            return status

        qty = int(position.get("qty") or 0)
        if qty <= 0:
            return status

        logger.warning(
            "%s: MFE/TIME EXIT TRIGGER | reason=%s minutes=%.1f mfe=%.3f%% mae=%.3f%% current=%.3f%% giveback=%.1f%% qty=%s",
            symbol, reason, minutes, mfe, mae, current_pct, giveback_pct, qty,
        )

        exit_result = trading_main.place_exit_order(
            kite,
            symbol,
            position["direction"],
            qty,
            exchange,
            cfg,
            protection_clearance=None,
        )
        confirmed_qty = int(exit_result.get("filled_quantity") or 0)
        if confirmed_qty <= 0:
            logger.warning("%s: MFE/TIME EXIT not filled | status=%s", symbol, exit_result.get("status"))
            return f"EXIT NOT FILLED ({reason})"

        exit_price = exit_result.get("average_price")
        if exit_price is None:
            exit_price = last_price
        exit_price = float(exit_price)

        cost_result = trading_main.net_pnl_for_trade(
            position["direction"],
            confirmed_qty,
            float(position["entry"]),
            exit_price,
        )
        gross_pnl = cost_result["gross_pnl"]
        costs = cost_result["costs"]
        net_pnl = cost_result["net_pnl"]

        risk.record_trade_result(net_pnl)
        analytics = trading_main._trade_analytics_from_position(position)
        analytics.update({
            "mfe_time_minutes": minutes,
            "mfe_time_current_pct": current_pct,
            "mfe_time_giveback_pct": giveback_pct,
            "mfe_time_exit_rule": reason,
        })
        trading_main.record_trade(
            symbol,
            position["direction"],
            confirmed_qty,
            float(position["entry"]),
            exit_price,
            net_pnl,
            reason,
            exchange=exchange,
            gross_pnl=gross_pnl,
            costs=costs,
            analytics=analytics,
        )

        remaining = qty - confirmed_qty
        if remaining <= 0:
            del open_positions[symbol]
        else:
            position["qty"] = remaining
            position["last_exit_price"] = exit_price
            position["last_exit_pnl"] = net_pnl
        trading_main.save_positions(open_positions)

        base._append({
            "event": "MFE_TIME_EXIT",
            "logged_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "paper_only": True,
            "reason": reason,
            "minutes_in_trade": minutes,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "current_pct": current_pct,
            "giveback_pct": giveback_pct,
            "exit_price": exit_price,
            "confirmed_qty": confirmed_qty,
            "net_pnl": net_pnl,
        })

        logger.info(
            "Closed %s (MFE/time: %s) confirmed_qty=%s exit=%.2f net P&L=%.2f | MFE=%.3f%% MAE=%.3f%% current=%.3f%% giveback=%.1f%% time=%.1f minutes",
            symbol, reason, confirmed_qty, exit_price, net_pnl, mfe, mae, current_pct, giveback_pct, minutes,
        )
        return f"CLOSED ({reason}) | net P&L {net_pnl:.2f}"

    trading_main.check_position_exit = check_position_exit
    logger.warning(
        "PAPER MFE/TIME EXIT ACTIVE: <%.0fm hold; %.0f-%.0fm selected lock/giveback; "
        ">=%.0fm dead-loser if MFE<%.2f%% and current<0; >%.0fm late giveback if MFE>=%.2f%%/%.0f%% giveback",
        MIN_HOLD_MINUTES,
        MIN_HOLD_MINUTES,
        MID_END_MINUTES,
        DEAD_TRADE_MINUTES,
        DEAD_TRADE_MAX_MFE_PCT,
        MID_END_MINUTES,
        LATE_MFE_PCT,
        GIVEBACK_PCT,
    )


def main():
    base.install_two_indicator_patch()

    import main as trading_main

    install_mfe_time_exit_patch(trading_main)
    trading_main.run()


if __name__ == "__main__":
    main()
