"""PAPER-only 5-minute Master Candlestick gate + full-capital sizing.

This module is intentionally installed at runtime by the dedicated PAPER
launcher.  It does not change LIVE behaviour and it does not submit orders.

Entry flow when installed:
    existing upstream signal (ADX/EMA/RSI + existing PA/MA path)
      -> approximate 5m OHLCV view resampled from the completed 3m entry bars
      -> Master Candlestick Engine
      -> NO_PATTERN: block
      -> WAITING: retain setup and upstream signal, no order
      -> CONFIRMED: copy geometric entry/SL/2R target onto the upstream signal
      -> existing main.py ranking / risk guard / executor

The 5m resampling deliberately matches the Aug-12 research harness that produced
FORTIS +Rs6.81.  It is NOT advertised as native Kite 5m.  A native-5m
production experiment should be a separate branch/test.

Sizing policy when installed:
    quantity = floor(CAPITAL / entry_price)

This makes up to 100% of PAPER cash capital available to one position.  The
launcher sets MAX_OPEN_POSITIONS=1, so the same cash capital is never allocated
to multiple concurrent positions.  No leverage is invented.  The existing
aggregate-risk and daily-loss guards remain binding and can still reject a
trade whose geometric stop risk is too large.
"""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass

import pandas as pd

import config as cfg
from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Trigger,
    evaluate_trade_entry,
)

logger = logging.getLogger("paper_5m_master_full_capital")

SOURCE_BAR_MINUTES = 3
MASTER_BAR_MINUTES = 5
MASTER_TICK_DEFAULT = 0.05

_ENGINE = CandlestickEngine(
    EngineConfig(
        risk_pct=0.20,
        min_rr=2.0,
        max_wait_bars=2,
    )
)
_PENDING_UPSTREAM_SIGNALS: dict[str, object] = {}


@dataclass(frozen=True)
class GateAudit:
    state: str
    pattern: str | None
    trigger: str | None
    reason: str


def reset_runtime_state() -> None:
    """Test/startup helper.  Clears only this module's in-memory PAPER state."""
    _ENGINE.pending.clear()
    _PENDING_UPSTREAM_SIGNALS.clear()


def _completed_3m_source(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    required = {"date", "open", "high", "low", "close", "volume"}
    if required - set(df.columns):
        return pd.DataFrame()
    out = df[["date", "open", "high", "low", "close", "volume"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    return out.sort_values("date").reset_index(drop=True)


def resample_completed_3m_to_5m(df_3m: pd.DataFrame) -> pd.DataFrame:
    """Match the Aug-12 3m->5m research geometry without source-bar look-ahead."""
    work = _completed_3m_source(df_3m)
    if work.empty:
        return work

    # main.py calls this from a completed-entry-candle scan.  The latest source
    # candle is therefore known only at its close.  Carry that availability
    # through the derived bar exactly as the replay harness did.
    work["source_available_at"] = work["date"] + pd.Timedelta(
        minutes=SOURCE_BAR_MINUTES
    )
    cutoff = work["source_available_at"].max()
    work = work.set_index("date")

    kwargs = dict(
        rule="5min",
        origin="start_day",
        offset="9h15min",
        label="left",
        closed="left",
    )
    bars = work.resample(**kwargs).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "source_available_at": "max",
        }
    )
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars = bars.rename(columns={"source_available_at": "available_at"}).reset_index()
    bars = bars.loc[pd.to_datetime(bars["available_at"]) <= pd.Timestamp(cutoff)]

    # Cash-session only.
    clock = bars["date"].dt.time
    session_start = pd.Timestamp("09:15").time()
    session_last_start = pd.Timestamp("15:25").time()
    bars = bars.loc[(clock >= session_start) & (clock <= session_last_start)]
    return bars.reset_index(drop=True)


def full_capital_quantity(capital: float, entry_price: float) -> int:
    """Whole-share quantity using at most 100% of PAPER cash capital."""
    try:
        capital = float(capital)
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(capital) or not math.isfinite(entry_price):
        return 0
    if capital <= 0 or entry_price <= 0:
        return 0
    return max(int(capital // entry_price), 0)


def _apply_plan_to_signal(signal, plan):
    updated = copy.copy(signal)
    updated.entry_price = float(plan.entry_price)
    updated.stop_loss = float(plan.stop_price)
    updated.target = float(plan.target_price)
    updated.reason = (
        f"{signal.reason} | PAPER 5m MASTER CANDLE={plan.pattern.value} "
        f"trigger={plan.trigger.value} geometricSL={plan.stop_price:.4f} "
        f"target2R={plan.target_price:.4f}"
    )
    # Analytics are additive and optional; main.py tolerates extra attrs.
    updated.master_candlestick_pattern = plan.pattern.value
    updated.master_candlestick_trigger = plan.trigger.value
    updated.master_candlestick_entry = float(plan.entry_price)
    updated.master_candlestick_stop = float(plan.stop_price)
    updated.master_candlestick_target = float(plan.target_price)
    return updated


def _evaluate_gate(symbol: str, df_entry: pd.DataFrame, direction: str, tick_size: float):
    master_df = resample_completed_3m_to_5m(df_entry)
    return evaluate_trade_entry(
        symbol,
        master_df,
        direction,
        float(getattr(cfg, "CAPITAL", 0.0) or 0.0),
        float(tick_size or MASTER_TICK_DEFAULT),
        _ENGINE,
    )


def install_on_trading_main(trading_main) -> None:
    """Patch only the PAPER process's imported main module."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: 5m master gate requires PAPER_TRADING=True")

    if getattr(trading_main.evaluate, "_paper_5m_master_wrapped", False):
        return

    original_evaluate = trading_main.evaluate

    def evaluate_with_master_gate(symbol, df_15m, df_entry, market_df_15m, cfg_obj):
        # A previously identified pattern is allowed to finish its two-bar
        # confirmation window even if the upstream 3m signal is not emitted on
        # the later scan.  Direction is frozen from the original upstream signal.
        pending_signal = _PENDING_UPSTREAM_SIGNALS.get(symbol)
        if pending_signal is not None:
            tick = MASTER_TICK_DEFAULT
            result = _evaluate_gate(
                symbol,
                df_entry,
                pending_signal.direction,
                tick,
            )
            if result.state == GateState.CONFIRMED and result.plan is not None:
                _PENDING_UPSTREAM_SIGNALS.pop(symbol, None)
                if result.plan.trigger == Trigger.NEXT_OPEN:
                    # The Aug-12 5m research excluded NEXT_OPEN P&L because a
                    # true bar-boundary execution path was not part of that
                    # experiment.  Preserve that tested/fail-closed behaviour.
                    logger.warning(
                        "%s: PAPER 5m MASTER blocked stale NEXT_OPEN confirmation (%s)",
                        symbol,
                        result.plan.pattern.value,
                    )
                    return None
                logger.warning(
                    "%s: PAPER 5m MASTER CONFIRMED pending %s | entry=%.4f SL=%.4f TP=%.4f",
                    symbol,
                    result.plan.pattern.value,
                    result.plan.entry_price,
                    result.plan.stop_price,
                    result.plan.target_price,
                )
                return _apply_plan_to_signal(pending_signal, result.plan)

            if result.state == GateState.WAITING:
                return None

            # The engine no longer has an actionable pending setup.
            if not _ENGINE.pending.get(symbol):
                _PENDING_UPSTREAM_SIGNALS.pop(symbol, None)

        upstream = original_evaluate(
            symbol,
            df_15m,
            df_entry,
            market_df_15m,
            cfg_obj,
        )
        if upstream is None:
            return None

        result = _evaluate_gate(
            symbol,
            df_entry,
            upstream.direction,
            MASTER_TICK_DEFAULT,
        )
        pattern = result.pattern.value if result.pattern is not None else None
        logger.info(
            "%s: PAPER 5m MASTER | direction=%s state=%s pattern=%s reason=%s",
            symbol,
            upstream.direction,
            result.state.value,
            pattern,
            result.reason,
        )

        if result.state == GateState.CONFIRMED and result.plan is not None:
            if result.plan.trigger == Trigger.NEXT_OPEN:
                logger.warning(
                    "%s: PAPER 5m MASTER blocked NEXT_OPEN until true 5m bar-boundary execution exists",
                    symbol,
                )
                return None
            return _apply_plan_to_signal(upstream, result.plan)

        if result.state == GateState.WAITING:
            _PENDING_UPSTREAM_SIGNALS[symbol] = copy.copy(upstream)
            return None

        return None

    evaluate_with_master_gate._paper_5m_master_wrapped = True
    evaluate_with_master_gate._paper_5m_master_original = original_evaluate
    trading_main.evaluate = evaluate_with_master_gate

    risk_cls = trading_main.RiskManager
    original_position_size = risk_cls.position_size
    if not getattr(original_position_size, "_paper_full_capital_wrapped", False):
        def position_size_full_capital(self, entry_price, stop_loss):
            if (
                bool(getattr(self.cfg, "PAPER_TRADING", False))
                and bool(getattr(self.cfg, "PAPER_FULL_CAPITAL_PER_TRADE", False))
            ):
                qty = full_capital_quantity(
                    float(getattr(self.cfg, "CAPITAL", 0.0) or 0.0),
                    entry_price,
                )
                logger.info(
                    "PAPER FULL-CAPITAL SIZING | capital=%.2f entry=%.4f qty=%s notional=%.2f stop=%s",
                    float(getattr(self.cfg, "CAPITAL", 0.0) or 0.0),
                    float(entry_price),
                    qty,
                    qty * float(entry_price),
                    stop_loss,
                )
                return qty
            return original_position_size(self, entry_price, stop_loss)

        position_size_full_capital._paper_full_capital_wrapped = True
        position_size_full_capital._paper_full_capital_original = original_position_size
        risk_cls.position_size = position_size_full_capital

    logger.warning(
        "PAPER 5m MASTER GATE ACTIVE: tested 3m->5m geometry; VWAP+EMA50 AND; volume>SMA20; 2R; wait<=2; NEXT_OPEN fail-closed"
    )
    logger.warning(
        "PAPER FULL-CAPITAL SIZING ACTIVE: up to 100%% cash capital per trade; no leverage; MAX_OPEN_POSITIONS should be 1"
    )
