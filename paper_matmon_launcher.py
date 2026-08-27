#!/usr/bin/env python3
"""Matmon HaElohim PAPER launcher: EMA9/21 -> DI agreement -> quote repricing."""
from __future__ import annotations
import logging
import runpy
import time
import pandas as pd

import config as cfg
import strategy
import depth_confirmation
from depth_confirmation import DepthConfirmation
from indicators import directional_indicators
from matmon_entry_policy import di_agrees
from matmon_quote_confirmation import evaluate_quote_window
from paper_contrarian_launcher import install_two_indicator_patch

logger = logging.getLogger("paper_matmon_launcher")


def enforce_settings():
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: Matmon launcher requires PAPER_TRADING=True")
    if not bool(getattr(cfg, "ENABLE_WS_CANDLES", False)):
        raise SystemExit("SAFETY BLOCK: Matmon requires existing MODE_FULL WebSocket ticks")
    cfg.MATMON_MODE = True
    cfg.MATMON_EMA_FAST = 9
    cfg.MATMON_EMA_SLOW = 21
    cfg.MATMON_DI_PERIOD = 14
    cfg.MATMON_QUOTE_WINDOW_SECONDS = 3.0
    cfg.MATMON_QUOTE_MAX_AGE_SECONDS = 2.0
    cfg.ENTRY_TIMEFRAME = "3minute"
    cfg.ENTRY_SCAN_SHORTLIST_SIZE = 120
    cfg.CAPITAL = 5000.0
    cfg.RISK_PER_TRADE_PCT = 2.0
    cfg.MAX_OPEN_POSITIONS = 3
    cfg.MAX_TRADES_PER_DAY = 7
    cfg.CHECK_MARGIN_BEFORE_ENTRY = False
    cfg.PROPOSED_CLEAN_PIPELINE = True
    cfg.ENABLE_FINAL_EMA_DISTANCE_GATE = False
    cfg.ENABLE_DEPTH_CONFIRMATION_GATE = True
    cfg.DEPTH_RAW_DIRECTION_ONLY = True
    cfg.DEPTH_REQUIRE_DIRECTIONAL_CONFIRMATION = True
    cfg.ENABLE_RVOL_FILTER = False
    cfg.ENABLE_200_EMA_FILTER = False
    cfg.ENABLE_EMA200_WATCHLIST = False
    cfg.ENABLE_ENTRY_TIMING_FILTER = False
    cfg.ENABLE_CONFIRMATION_QUALITY_FILTER = False
    cfg.ENABLE_VOLUME_ACCELERATION_FILTER = False
    cfg.PAPER_PRICE_ACTION_OBSERVATIONAL = True


def install_matmon_policy():
    install_two_indicator_patch()
    # The base clean-pipeline patch enables some research gates. Matmon makes
    # those observational; re-assert the isolated Matmon settings afterwards.
    enforce_settings()
    ema_evaluate = strategy.evaluate

    def evaluate_with_di(symbol, df_15m, df_entry, df_index, cfg_obj):
        signal = ema_evaluate(symbol, df_15m, df_entry, df_index, cfg_obj)
        if signal is None:
            return None
        period = int(getattr(cfg_obj, "MATMON_DI_PERIOD", 14))
        pdi, mdi, _ = directional_indicators(df_entry, period)
        p = pd.to_numeric(pdi, errors="coerce").dropna()
        m = pd.to_numeric(mdi, errors="coerce").dropna()
        if p.empty or m.empty:
            logger.info("MATMON REJECT | %s | DI_UNAVAILABLE", symbol)
            return None
        plus_di = float(p.iloc[-1]); minus_di = float(m.iloc[-1])
        if not di_agrees(signal.direction, plus_di, minus_di):
            logger.info("MATMON REJECT | %s | direction=%s +DI=%.3f -DI=%.3f | DI_DISAGREES",
                        symbol, signal.direction, plus_di, minus_di)
            return None
        detail = dict(getattr(signal, "price_action_detail", {}) or {})
        detail["matmon"] = {"plus_di": plus_di, "minus_di": minus_di, "di_agree": True}
        signal.price_action_detail = detail
        logger.info("MATMON DI PASS | %s | direction=%s +DI=%.3f -DI=%.3f",
                    symbol, signal.direction, plus_di, minus_di)
        return signal

    strategy.evaluate = evaluate_with_di

    def evaluate_matmon_quote(ws_engine, symbol, direction, planned_quantity, cfg_obj, *, now=None):
        ticker = getattr(ws_engine, "ws_ticker", None) if ws_engine is not None else None
        buffer = getattr(ticker, "tick_buffer", None) if ticker is not None else None
        if buffer is None:
            return DepthConfirmation(False, "UNAVAILABLE", "MATMON_NO_TICKS")
        evidence = evaluate_quote_window(
            buffer, symbol, direction,
            window_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_WINDOW_SECONDS", 3.0)),
            max_age_seconds=float(getattr(cfg_obj, "MATMON_QUOTE_MAX_AGE_SECONDS", 2.0)),
            now=now,
        )
        logger.info("MATMON QUOTE | %s | %s | first=%s/%s last=%s/%s | %s",
                    symbol, direction, evidence.first_bid, evidence.first_ask,
                    evidence.last_bid, evidence.last_ask, evidence.reason)
        current = time.time() if now is None else float(now)
        return DepthConfirmation(
            bool(evidence.confirmed),
            "CONFIRMED" if evidence.confirmed else "MATMON_REJECT",
            evidence.reason,
            sample_count=2 if evidence.available else 0,
            coverage_seconds=(max(0.0, evidence.last_received_at - evidence.first_received_at)
                              if evidence.first_received_at is not None and evidence.last_received_at is not None else 0.0),
            latest_age_seconds=(max(0.0, current - evidence.last_received_at)
                                if evidence.last_received_at is not None else None),
        )

    depth_confirmation.evaluate_live_depth = evaluate_matmon_quote


def main():
    enforce_settings()
    install_matmon_policy()
    logger.critical(
        "MATMON HAELOHIM PAPER MODE ACTIVE | capital=5000 risk=2%% max_open=3 max_trades=7 | "
        "ENTRY=EMA9/21 + DI AGREEMENT + 3s BID/ASK REPRICING | NO REAL ORDERS"
    )
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
