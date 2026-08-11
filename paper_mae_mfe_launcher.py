#!/usr/bin/env python3
"""Paper-only ADX/EMA/RSI entry + MAE adverse-trend + MFE/time exits.

Exit precedence:
1. Native emergency stop / fixed target / hybrid target.
2. MAE early-failure exit after >10 minutes of sustained adverse EMA trend.
3. Existing selected MFE + time giveback/dead-trade model.

Current PAPER MAE early-failure rule (all conditions required):
- time in trade > 10 minutes
- MAE <= -0.30%
- current P&L <= -0.15%
- MFE < +0.30%
- adverse EMA trend for 3 consecutive COMPLETED 3-minute candles (~9 minutes)

Adverse trend definition:
- BUY: close < EMA9 AND EMA9 < EMA21
- SELL: close > EMA9 AND EMA9 > EMA21

This launcher is PAPER ONLY and never changes live trading behavior.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

import config as cfg
import paper_contrarian_launcher as base
import paper_mfe_time_launcher as mfe_time

logger = logging.getLogger("paper_mae_mfe_launcher")

MAE_MIN_AGE_MINUTES = float(getattr(cfg, "PAPER_MAE_MIN_AGE_MINUTES", 10.0))
MAE_THRESHOLD_PCT = float(getattr(cfg, "PAPER_MAE_THRESHOLD_PCT", -0.30))
CURRENT_LOSS_THRESHOLD_PCT = float(
    getattr(cfg, "PAPER_MAE_CURRENT_LOSS_THRESHOLD_PCT", -0.15)
)
MAX_MFE_FOR_FAILURE_PCT = float(
    getattr(cfg, "PAPER_MAE_MAX_MFE_FAILURE_PCT", 0.30)
)
ADVERSE_CANDLES_REQUIRED = int(
    getattr(cfg, "PAPER_MAE_ADVERSE_CANDLES_REQUIRED", 3)
)
EMA_FAST = 9
EMA_SLOW = 21


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


def _completed_entry_candles(df, trading_main):
    """Return only completed entry-timeframe candles, with EMA9/EMA21."""
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work["date"] = pd.to_datetime(work["date"])
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["date", "close"])
    if work.empty:
        return work

    interval_min = trading_main.candle_interval_minutes(cfg.ENTRY_TIMEFRAME)
    tz = getattr(work["date"].dt, "tz", None)
    now = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    completed = work.loc[
        work["date"] + pd.Timedelta(minutes=interval_min) <= now
    ].copy()
    if completed.empty:
        return completed

    # Calculate EMA on all completed intraday history so the final candles do
    # not suffer from a short warm-up window.
    completed["mae_exit_ema9"] = completed["close"].ewm(
        span=EMA_FAST, adjust=False
    ).mean()
    completed["mae_exit_ema21"] = completed["close"].ewm(
        span=EMA_SLOW, adjust=False
    ).mean()
    return completed


def _adverse_trend(position, completed, trading_main):
    """Require configured consecutive completed candles in adverse EMA trend."""
    if completed is None or len(completed) < ADVERSE_CANDLES_REQUIRED:
        return False, {}

    recent = completed.tail(ADVERSE_CANDLES_REQUIRED).copy()
    direction = position.get("direction")

    if direction == "BUY":
        flags = (
            (recent["close"] < recent["mae_exit_ema9"])
            & (recent["mae_exit_ema9"] < recent["mae_exit_ema21"])
        )
        adverse_label = "BUY_DOWN_TREND"
    elif direction == "SELL":
        flags = (
            (recent["close"] > recent["mae_exit_ema9"])
            & (recent["mae_exit_ema9"] > recent["mae_exit_ema21"])
        )
        adverse_label = "SELL_UP_TREND"
    else:
        return False, {}

    interval_min = trading_main.candle_interval_minutes(cfg.ENTRY_TIMEFRAME)
    detail = {
        "adverse_label": adverse_label,
        "required_consecutive_candles": ADVERSE_CANDLES_REQUIRED,
        "observed_consecutive_candles": int(flags.sum()) if bool(flags.all()) else 0,
        "adverse_duration_minutes": ADVERSE_CANDLES_REQUIRED * interval_min,
        "candles": [
            {
                "date": str(row["date"]),
                "close": float(row["close"]),
                "ema9": float(row["mae_exit_ema9"]),
                "ema21": float(row["mae_exit_ema21"]),
                "adverse": bool(flag),
            }
            for (_, row), flag in zip(recent.iterrows(), flags.tolist())
        ],
    }
    return bool(flags.all()), detail


def install_mae_adverse_exit_patch(trading_main):
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: MAE adverse-trend exit requires PAPER_TRADING=True")

    original_check = trading_main.check_position_exit

    def check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
        # Native emergency stop / target / hybrid handling always gets priority.
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
        if minutes is None or minutes <= MAE_MIN_AGE_MINUTES:
            return status

        token = tokens.get(symbol)
        if token is None:
            return status
        exchange = position.get("exchange", exchange_map.get(symbol, "NSE"))

        try:
            df = trading_main.fetch_candles(
                kite,
                token,
                cfg.ENTRY_TIMEFRAME,
                lookback_days=1,
                trim_incomplete=False,
            )
            time.sleep(0.35)
            if df is None or df.empty:
                return status

            completed = _completed_entry_candles(df, trading_main)
            if completed.empty:
                return status

            last_price = float(df.iloc[-1]["close"])
            entry_dt = pd.to_datetime(position.get("entry_time")) if position.get("entry_time") else None
            df_since = df
            if entry_dt is not None and "date" in df.columns:
                dates = pd.to_datetime(df["date"])
                try:
                    candidate = df.loc[dates >= entry_dt]
                    if not candidate.empty:
                        df_since = candidate
                except Exception:
                    pass

            mfe, mae, current_pct, giveback_pct = mfe_time._excursions(
                position, df_since, last_price
            )
            adverse, adverse_detail = _adverse_trend(
                position, completed, trading_main
            )
        except Exception as exc:
            logger.warning("%s: MAE adverse-trend observation failed: %s", symbol, exc)
            return status

        position["mfe_pct"] = mfe
        position["mae_pct"] = mae
        position["mae_exit_current_pct"] = current_pct
        position["mae_exit_minutes"] = minutes
        position["mae_exit_adverse_trend"] = adverse
        position["mae_exit_adverse_detail"] = adverse_detail
        trading_main.save_positions(open_positions)

        trigger = (
            mae <= MAE_THRESHOLD_PCT
            and current_pct <= CURRENT_LOSS_THRESHOLD_PCT
            and mfe < MAX_MFE_FOR_FAILURE_PCT
            and adverse
        )

        if not trigger:
            logger.info(
                "%s: MAE/TREND HOLD | minutes=%.1f mae=%.3f%% mfe=%.3f%% current=%.3f%% adverse=%s",
                symbol, minutes, mae, mfe, current_pct, adverse,
            )
            return status

        reason = "mae_adverse_trend_10m"
        qty = int(position.get("qty") or 0)
        if qty <= 0:
            return status

        logger.warning(
            "%s: MAE/TREND EXIT TRIGGER | minutes=%.1f mae=%.3f%% mfe=%.3f%% current=%.3f%% adverse=%s qty=%s",
            symbol, minutes, mae, mfe, current_pct, adverse, qty,
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
            logger.warning(
                "%s: MAE/TREND EXIT not filled | status=%s",
                symbol,
                exit_result.get("status"),
            )
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
            "mae_exit_rule": reason,
            "mae_exit_minutes": minutes,
            "mae_exit_current_pct": current_pct,
            "mae_exit_mfe_pct": mfe,
            "mae_exit_mae_pct": mae,
            "mae_exit_adverse_detail": adverse_detail,
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
            "event": "MAE_ADVERSE_TREND_EXIT",
            "logged_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "paper_only": True,
            "reason": reason,
            "minutes_in_trade": minutes,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "current_pct": current_pct,
            "giveback_pct": giveback_pct,
            "adverse_trend": adverse_detail,
            "exit_price": exit_price,
            "confirmed_qty": confirmed_qty,
            "net_pnl": net_pnl,
        })

        logger.info(
            "Closed %s (MAE/adverse trend) confirmed_qty=%s exit=%.2f net P&L=%.2f | MAE=%.3f%% MFE=%.3f%% current=%.3f%% time=%.1f minutes",
            symbol, confirmed_qty, exit_price, net_pnl, mae, mfe, current_pct, minutes,
        )
        return f"CLOSED ({reason}) | net P&L {net_pnl:.2f}"

    trading_main.check_position_exit = check_position_exit
    logger.warning(
        "PAPER MAE ADVERSE-TREND EXIT ACTIVE: >%.0fm AND MAE<=%.2f%% AND current<=%.2f%% "
        "AND MFE<%.2f%% AND %s consecutive completed %s candles adverse to trade",
        MAE_MIN_AGE_MINUTES,
        MAE_THRESHOLD_PCT,
        CURRENT_LOSS_THRESHOLD_PCT,
        MAX_MFE_FOR_FAILURE_PCT,
        ADVERSE_CANDLES_REQUIRED,
        cfg.ENTRY_TIMEFRAME,
    )


def main():
    base.install_two_indicator_patch()

    import main as trading_main

    # Install MAE first. The MFE wrapper is then installed outside it, so
    # effective precedence becomes native emergency stop/target -> MAE -> MFE/time.
    install_mae_adverse_exit_patch(trading_main)
    mfe_time.install_mfe_time_exit_patch(trading_main)

    logger.warning(
        "PAPER EXIT STACK ACTIVE: emergency stop/target -> MAE adverse-trend -> MFE/time"
    )
    trading_main.run()


if __name__ == "__main__":
    main()
