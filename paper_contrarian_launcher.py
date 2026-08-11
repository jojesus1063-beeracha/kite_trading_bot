#!/usr/bin/env python3
"""Paper-only EMA9/EMA21 + RSI directional strategy.

ONLY two indicators may determine BUY/SELL:
1) EMA9/EMA21 on completed entry candles gives the REVERSED base direction:
   - EMA9 > EMA21 -> SELL
   - EMA9 < EMA21 -> BUY
2) RSI(14) is evaluated second:
   - RSI >= 70 -> BUY override
   - RSI <= 30 -> SELL override
   - 30 < RSI < 70 -> PASS; keep reversed EMA9/EMA21 direction

All other technical/market inputs are observational only in this launcher and
cannot reject or reverse a signal. Existing execution safety, position sizing,
stop/target handling, daily limits, paper-only protection and square-off remain
outside the indicator decision and continue to apply.
"""

from __future__ import annotations

import logging
import runpy
from datetime import datetime

import pandas as pd

import config as cfg
import strategy

logger = logging.getLogger("paper_contrarian_launcher")

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD):
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


def ema_direction(df: pd.DataFrame):
    """Reversed EMA9/21 base direction on the latest completed entry candle."""
    if df is None or df.empty or "close" not in df.columns or len(df) < EMA_SLOW:
        return None, None, None
    close = pd.to_numeric(df["close"], errors="coerce")
    ema9 = close.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    ema21 = close.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return None, None, None
    if ema9 > ema21:
        return "SELL", float(ema9), float(ema21)
    if ema9 < ema21:
        return "BUY", float(ema9), float(ema21)
    return None, float(ema9), float(ema21)


def rsi_direction(rsi):
    """Extreme RSI overrides; None means PASS to reversed EMA direction."""
    if rsi is None:
        return None
    if rsi >= RSI_OVERBOUGHT:
        return "BUY"
    if rsi <= RSI_OVERSOLD:
        return "SELL"
    return None


def _within_entry_window(ts) -> bool:
    try:
        current = pd.Timestamp(ts).time()
        start = datetime.strptime(str(getattr(cfg, "NO_ENTRY_BEFORE", "09:25")), "%H:%M").time()
        end = datetime.strptime(str(getattr(cfg, "NO_ENTRY_AFTER", "15:00")), "%H:%M").time()
        return start <= current <= end
    except Exception:
        return False


def _observational_snapshot(df_15m, df_entry, df_index_15m):
    """Log useful values only. Nothing returned here can block a signal."""
    obs = {}
    try:
        if df_15m is not None and not df_15m.empty:
            row = df_15m.iloc[-1]
            for key in ("vwap", "adx", "ema_fast", "ema_slow", "ema200"):
                value = row.get(key)
                if value is not None and not pd.isna(value):
                    obs[key] = round(float(value), 6)
    except Exception:
        pass
    try:
        if df_entry is not None and not df_entry.empty:
            row = df_entry.iloc[-1]
            for key in ("volume", "avg_volume"):
                value = row.get(key)
                if value is not None and not pd.isna(value):
                    obs[key] = round(float(value), 6)
    except Exception:
        pass
    try:
        if df_index_15m is not None and not df_index_15m.empty:
            row = df_index_15m.iloc[-1]
            for key in ("close", "ema_fast", "ema_slow", "adx"):
                value = row.get(key)
                if value is not None and not pd.isna(value):
                    obs[f"index_{key}"] = round(float(value), 6)
    except Exception:
        pass
    return obs


def install_two_indicator_patch() -> None:
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: EMA/RSI launcher requires PAPER_TRADING=True")

    def two_indicator_evaluate(symbol, df_15m, df_5m, df_index_15m, cfg_obj):
        if df_5m is None or df_5m.empty:
            return None

        curr = df_5m.iloc[-1]
        if "date" not in curr or not _within_entry_window(curr["date"]):
            return None

        base_direction, ema9, ema21 = ema_direction(df_5m)
        if base_direction is None:
            logger.info("PAPER TWO-INDICATOR | %s | EMA9/21 unavailable/flat", symbol)
            return None

        rsi = calculate_rsi(df_5m)
        rsi_override = rsi_direction(rsi)
        final_direction = rsi_override or base_direction

        entry = float(curr["close"])
        if entry <= 0:
            return None

        stop_pct = float(getattr(cfg_obj, "STOP_LOSS_PERCENT", 0.45)) / 100.0
        target_pct = float(getattr(cfg_obj, "PROFIT_TARGET_PERCENT", 0.70)) / 100.0
        if final_direction == "BUY":
            stop = entry * (1.0 - stop_pct)
            target = entry * (1.0 + target_pct)
        else:
            stop = entry * (1.0 + stop_pct)
            target = entry * (1.0 - target_pct)

        obs = _observational_snapshot(df_15m, df_5m, df_index_15m)
        rsi_text = "NA" if rsi is None else f"{rsi:.2f}"
        mode = "RSI_OVERRIDE" if rsi_override is not None else "RSI_PASS"
        logger.info(
            "PAPER TWO-INDICATOR | %s | EMA9=%.4f EMA21=%.4f base=%s | RSI=%s %s | FINAL=%s | OBS=%s",
            symbol, ema9, ema21, base_direction, rsi_text, mode, final_direction, obs,
        )

        reason = (
            f"PAPER TWO-INDICATOR | REVERSED EMA9={ema9:.4f} EMA21={ema21:.4f} -> {base_direction} | "
            f"RSI({RSI_PERIOD})={rsi_text} {mode} -> {final_direction} | "
            "all other indicators observational"
        )
        return strategy.Signal(
            symbol=symbol,
            direction=final_direction,
            entry_price=entry,
            stop_loss=stop,
            target=target,
            timestamp=curr["date"],
            reason=reason,
            confidence=None,
        )

    strategy.evaluate = two_indicator_evaluate
    logger.warning(
        "PAPER TWO-INDICATOR MODE ACTIVE: REVERSED EMA9/21 base (EMA9>21 SELL, EMA9<21 BUY); RSI>=70 BUY override; RSI<=30 SELL override; RSI30-70 PASS; all other indicators observational"
    )


def main() -> None:
    install_two_indicator_patch()
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
