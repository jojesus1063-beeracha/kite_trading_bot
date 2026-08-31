#!/usr/bin/env python3
"""Matmon HaElohim PAPER launcher.

Entry authorization is intentionally narrow and fail-closed:
completed 3-minute EMA3/EMA15 direction -> DI(14) agreement -> fresh 3-second
full-path CLEAN quote confirmation -> LTP velocity + weighted-5 direction +
weighted-5 strengthening. Legacy research filters remain observational only.
Operational/risk guards in main remain responsible for safe paper execution.
"""
from __future__ import annotations

import logging
import runpy
import time

import pandas as pd

import config as cfg
import strategy
import depth_confirmation
from depth_confirmation import DepthConfirmation
from indicators import directional_indicators, ema
from matmon_entry_policy import evaluate_direction
from matmon_quote_confirmation import evaluate_quote_window
from matmon_microstructure import evaluate_microstructure

logger = logging.getLogger("paper_matmon_launcher")

MATMON_REQUIRED = {
    "PAPER_TRADING": True,
    "ENABLE_WS_CANDLES": True,
    "MATMON_MODE": True,
    "MATMON_EMA_FAST": 3,
    "MATMON_EMA_SLOW": 15,
    "MATMON_DI_PERIOD": 14,
    "MATMON_QUOTE_WINDOW_SECONDS": 3.0,
    "MATMON_QUOTE_MAX_AGE_SECONDS": 2.0,
    "ENTRY_TIMEFRAME": "3minute",
    "ENTRY_SCAN_SHORTLIST_SIZE": 120,
    "CAPITAL": 5000.0,
    "RISK_PER_TRADE_PCT": 2.0,
    "MAX_POSITION_SIZE_PCT": 20.0,
    "MAX_OPEN_POSITIONS": 5,
    "MAX_TRADES_PER_DAY": 100,
    "CHECK_MARGIN_BEFORE_ENTRY": True,
}

MATMON_FORBIDDEN_TRUE = (
    "ENABLE_FINAL_EMA_DISTANCE_GATE",
    "ENABLE_RVOL_FILTER",
    "ENABLE_200_EMA_FILTER",
    "ENABLE_EMA200_WATCHLIST",
    "ENABLE_ENTRY_TIMING_FILTER",
    "ENABLE_CONFIRMATION_QUALITY_FILTER",
    "ENABLE_VOLUME_ACCELERATION_FILTER",
    "PAPER_REQUIRE_VALIDATED_BREAKOUT",
    "PAPER_REQUIRE_EMA200_ALIGNMENT",
    "PAPER_REQUIRE_INDEPENDENT_CONFIRMATION",
    "PAPER_ENABLE_COST_AWARE_GATE",
)


def enforce_settings():
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: Matmon launcher requires PAPER_TRADING=True")
    if not bool(getattr(cfg, "ENABLE_WS_CANDLES", False)):
        raise SystemExit("SAFETY BLOCK: Matmon requires MODE_FULL WebSocket ticks")

    cfg.MATMON_MODE = True
    cfg.MATMON_EMA_FAST = 3
    cfg.MATMON_EMA_SLOW = 15
    cfg.MATMON_DI_PERIOD = 14
    cfg.MATMON_QUOTE_WINDOW_SECONDS = 3.0
    cfg.MATMON_QUOTE_MAX_AGE_SECONDS = 2.0
    cfg.ENTRY_TIMEFRAME = "3minute"
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = 120

    cfg.CAPITAL = 5000.0
    cfg.RISK_PER_TRADE_PCT = 2.0
    cfg.MAX_POSITION_SIZE_PCT = 20.0
    cfg.MAX_OPEN_POSITIONS = 5
    cfg.MAX_TRADES_PER_DAY = 100
    cfg.CHECK_MARGIN_BEFORE_ENTRY = True

    cfg.PROPOSED_CLEAN_PIPELINE = True
    cfg.ENABLE_DEPTH_CONFIRMATION_GATE = True
    cfg.DEPTH_RAW_DIRECTION_ONLY = True
    cfg.DEPTH_REQUIRE_DIRECTIONAL_CONFIRMATION = True
    cfg.PAPER_PRICE_ACTION_OBSERVATIONAL = True
    cfg.PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS = 0.0

    for name in MATMON_FORBIDDEN_TRUE:
        setattr(cfg, name, False)


def assert_runtime_contract():
    errors = []
    for name, expected in MATMON_REQUIRED.items():
        actual = getattr(cfg, name, None)
        if actual != expected:
            errors.append(f"{name}={actual!r}, expected {expected!r}")

    for name in MATMON_FORBIDDEN_TRUE:
        if bool(getattr(cfg, name, False)):
            errors.append(f"{name}=True (legacy entry veto must be disabled)")

    if not bool(getattr(cfg, "ENABLE_DEPTH_CONFIRMATION_GATE", False)):
        errors.append("ENABLE_DEPTH_CONFIRMATION_GATE=False (Matmon confirmation hook would not run)")
    if float(getattr(cfg, "PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS", 0.0) or 0.0) != 0.0:
        errors.append("PAPER_DELAYED_ENTRY_CONFIRMATION_SECONDS must be 0 for Matmon")

    if errors:
        raise SystemExit("MATMON RUNTIME CONTRACT FAILED:\n - " + "\n - ".join(errors))

    logger.critical(
        "MATMON RUNTIME CONTRACT PASS | ENTRY AUTHORIZATION ONLY: "
        "EMA3/15 -> DI(14) -> 3s FULL-PATH CLEAN -> LTP velocity -> "
        "weighted-5 direction -> weighted-5 strengthening | legacy research vetoes disabled"
    )


def _latest_finite(series):
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    value = float(values.iloc[-1])
    return value if pd.notna(value) else None


def _matmon_signal(symbol, df_entry, cfg_obj):
    """Build a raw Matmon signal from one completed 3-minute candle history."""
    if df_entry is None or df_entry.empty:
        logger.info("MATMON REJECT | %s | NO_ENTRY_CANDLES", symbol)
        return None

    required = {"date", "high", "low", "close"}
    if not required.issubset(df_entry.columns):
        logger.info("MATMON REJECT | %s | ENTRY_COLUMNS_UNAVAILABLE", symbol)
        return None

    fast_period = int(getattr(cfg_obj, "MATMON_EMA_FAST", 3))
    slow_period = int(getattr(cfg_obj, "MATMON_EMA_SLOW", 15))
    di_period = int(getattr(cfg_obj, "MATMON_DI_PERIOD", 14))
    if (fast_period, slow_period, di_period) != (3, 15, 14):
        logger.error(
            "MATMON REJECT | %s | INVALID_RUNTIME_PERIODS fast=%s slow=%s di=%s",
            symbol, fast_period, slow_period, di_period,
        )
        return None

    ema3 = _latest_finite(ema(df_entry, fast_period))
    ema15 = _latest_finite(ema(df_entry, slow_period))
    pdi, mdi, _ = directional_indicators(df_entry, di_period)
    plus_di = _latest_finite(pdi)
    minus_di = _latest_finite(mdi)

    decision = evaluate_direction(
        ema3=ema3,
        ema15=ema15,
        plus_di=plus_di,
        minus_di=minus_di,
    )
    if not decision.accepted:
        logger.info(
            "MATMON REJECT | %s | EMA3=%s EMA15=%s +DI=%s -DI=%s | %s",
            symbol, ema3, ema15, plus_di, minus_di, decision.reason,
        )
        return None

    cur = df_entry.iloc[-1]
    try:
        entry = float(cur["close"])
    except (TypeError, ValueError, KeyError):
        entry = 0.0
    if not pd.notna(entry) or entry <= 0:
        logger.info("MATMON REJECT | %s | INVALID_ENTRY_PRICE", symbol)
        return None

    direction = decision.direction
    stop_fraction = float(getattr(cfg_obj, "STOP_LOSS_PERCENT", 0.45)) / 100.0
    target_fraction = float(getattr(cfg_obj, "PROFIT_TARGET_PERCENT", 0.70)) / 100.0
    sign = 1.0 if direction == "BUY" else -1.0
    stop = entry * (1.0 - sign * stop_fraction)
    target = entry * (1.0 + sign * target_fraction)

    logger.info(
        "MATMON EMA+DI PASS | %s | EMA3=%.6f EMA15=%.6f +DI=%.3f -DI=%.3f direction=%s",
        symbol, ema3, ema15, plus_di, minus_di, direction,
    )
    return strategy.Signal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        target=target,
        timestamp=cur["date"],
        reason="MATMON EMA3/EMA15 + DI14 RAW SIGNAL",
        confidence="MATMON_EMA_DI",
        price_action_detail={
            "matmon": {
                "ema3": ema3,
                "ema15": ema15,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "direction_reason": decision.reason,
            }
        },
    )


def install_matmon_policy():
    """Install only the Matmon direction and confirmation hooks; no legacy strategy patch."""
    enforce_settings()

    def evaluate_matmon(symbol, df_15m, df_entry, df_index, cfg_obj):
        del df_15m, df_index
        return _matmon_signal(symbol, df_entry, cfg_obj)

    strategy.evaluate = evaluate_matmon

    def evaluate_matmon_quote(ws_engine, symbol, direction, planned_quantity, cfg_obj, *, now=None):
        del planned_quantity
        ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine is not None else None
        buffer = getattr(ticker, "tick_buffer", None) if ticker is not None else None
        if buffer is None:
            return DepthConfirmation(False, "UNAVAILABLE", "MATMON_NO_TICKS")

        clean = evaluate_quote_window(
            buffer,
            symbol,
            direction,
            window_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_WINDOW_SECONDS", 3.0)),
            max_age_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_MAX_AGE_SECONDS", 2.0)),
            now=now,
        )
        if not clean.confirmed:
            logger.info(
                "MATMON CONFIRM REJECT | %s | %s | CLEAN=%s | first=%s/%s last=%s/%s",
                symbol, direction, clean.reason,
                clean.first_bid, clean.first_ask, clean.last_bid, clean.last_ask,
            )
            current = time.time() if now is None else float(now)
            return DepthConfirmation(
                False,
                "MATMON_REJECT",
                clean.reason,
                sample_count=len(clean.ticks),
                coverage_seconds=(
                    max(0.0, clean.last_received_at - clean.first_received_at)
                    if clean.first_received_at is not None and clean.last_received_at is not None
                    else 0.0
                ),
                latest_age_seconds=(
                    max(0.0, current - clean.last_received_at)
                    if clean.last_received_at is not None else None
                ),
            )

        micro = evaluate_microstructure(direction, clean.ticks)
        accepted = bool(micro.accepted)
        logger.info(
            "MATMON MICROSTRUCTURE | %s | %s | accepted=%s velocity=%s w5=%s w5_change=%s | %s",
            symbol, direction, accepted, micro.ltp_velocity_per_sec,
            micro.last_weighted_5_imbalance, micro.weighted_5_imbalance_change, micro.reason,
        )
        current = time.time() if now is None else float(now)
        return DepthConfirmation(
            accepted,
            "CONFIRMED" if accepted else "MATMON_REJECT",
            "MATMON_CLEAN_AND_MICROSTRUCTURE_CONFIRMED" if accepted else micro.reason,
            sample_count=len(clean.ticks),
            coverage_seconds=(
                max(0.0, clean.last_received_at - clean.first_received_at)
                if clean.first_received_at is not None and clean.last_received_at is not None
                else 0.0
            ),
            latest_age_seconds=(
                max(0.0, current - clean.last_received_at)
                if clean.last_received_at is not None else None
            ),
            median_imbalance=micro.last_weighted_5_imbalance,
        )

    depth_confirmation.evaluate_live_depth = evaluate_matmon_quote
    assert_runtime_contract()


def main():
    enforce_settings()
    install_matmon_policy()
    logger.critical(
        "MATMON HAELOHIM PAPER MODE ACTIVE | capital=5000 risk=2%% max_position=20%% max_open=5 max_trades=100 | "
        "ENTRY=EMA3/15 + DI14 + 3s FULL-PATH CLEAN + LTP velocity + W5 direction + W5 strengthening | "
        "LEGACY ENTRY VETOES OFF | NO REAL ORDERS"
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
