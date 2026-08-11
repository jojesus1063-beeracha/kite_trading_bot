#!/usr/bin/env python3
"""Launch the normal bot in paper-only RSI-directed contrarian mode.

Safety properties:
- Refuses to run unless config.PAPER_TRADING is True.
- Does not modify strategy.py or live service behavior.
- RSI(14) on completed entry candles supplies the BUY/SELL direction:
    RSI >= 70 -> SELL search
    RSI <= 30 -> BUY search
    30 < RSI < 70 -> no directional signal
- The existing confirmation, risk, stop, target and execution pipeline remains active.
"""

from __future__ import annotations

import logging
import runpy

import pandas as pd

import config as cfg
import strategy

logger = logging.getLogger("paper_contrarian_launcher")

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD):
    """Return Wilder-style RSI from the latest completed entry candles."""
    if df is None or df.empty or "close" not in df.columns or len(df) < period + 1:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    if pd.isna(avg_gain.iloc[-1]) or pd.isna(avg_loss.iloc[-1]):
        return None
    if avg_loss.iloc[-1] == 0:
        return 100.0 if avg_gain.iloc[-1] > 0 else 50.0
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
    return float(100.0 - (100.0 / (1.0 + rs)))


def rsi_direction(rsi):
    if rsi is None:
        return None
    if rsi >= RSI_OVERBOUGHT:
        return "SELL"
    if rsi <= RSI_OVERSOLD:
        return "BUY"
    return None


def install_contrarian_patch() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: RSI-directed launcher requires PAPER_TRADING=True")

    original_evaluate = strategy.evaluate

    def rsi_directed_evaluate(*args, **kwargs):
        # strategy.evaluate receives the entry-candle dataframe as df_5m in the
        # current engine even when ENTRY_TIMEFRAME is configured as 3minute.
        df_entry = kwargs.get("df_5m")
        if df_entry is None and len(args) >= 3:
            df_entry = args[2]
        rsi = calculate_rsi(df_entry)
        direction = rsi_direction(rsi)
        if direction is None:
            logger.info("PAPER RSI DIRECTION | RSI=%s | neutral/no trade", "NA" if rsi is None else f"{rsi:.2f}")
            return None

        # Preserve the normal strategy's confirmation/risk pipeline, but make
        # its trend input agree with the RSI-selected side for this call only.
        original_get_trend = strategy.get_trend
        desired_trend = "UP" if direction == "BUY" else "DOWN"

        def rsi_get_trend(row_15m, cfg=None, require_vwap=True):
            # First require the underlying trend calculation to be valid; then
            # replace only its direction in this paper experiment.
            normal = original_get_trend(row_15m, cfg, require_vwap=require_vwap)
            return desired_trend if normal is not None else None

        strategy.get_trend = rsi_get_trend
        try:
            signal = original_evaluate(*args, **kwargs)
        finally:
            strategy.get_trend = original_get_trend

        if signal is not None:
            signal.reason = f"PAPER RSI({RSI_PERIOD})={rsi:.2f} -> {direction} | " + str(signal.reason)
        return signal

    strategy.evaluate = rsi_directed_evaluate
    logger.warning(
        "PAPER RSI DIRECTION ACTIVE: RSI >= %.0f -> SELL; RSI <= %.0f -> BUY; neutral -> no trade",
        RSI_OVERBOUGHT,
        RSI_OVERSOLD,
    )


def main() -> None:
    install_contrarian_patch()
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
