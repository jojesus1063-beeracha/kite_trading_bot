"""PAPER-only native-5m candlestick final gate + full-capital sizing.

This module is deliberately installed only by the PAPER launcher.  It leaves
LIVE behavior untouched.

Runtime order after installation:
    upstream ADX/EMA/RSI direction
    -> Price Action hard gate
    -> Market Alignment hard gate
    -> native Kite 5-minute Master Candlestick Engine
    -> full available PAPER-capital sizing (symbol-specific MIS margin when
       Kite can price it; cash-only fallback when it cannot)
    -> existing aggregate daily-risk guard
    -> existing PAPER executor

The candlestick engine remains direction-only: it can confirm/wait/block the
upstream BUY/SELL direction, but can never reverse it.
"""
from __future__ import annotations

from dataclasses import replace
import importlib
import logging
import math
from typing import Optional

import config as cfg
from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    evaluate_trade_entry,
)

logger = logging.getLogger("paper_native_5m_candlestick_full_capital")

PAPER_CANDLE_INTERVAL = "5minute"
PAPER_CAPITAL_ALLOCATION_PCT = 100.0
PAPER_MAX_OPEN_POSITIONS = 1
PAPER_CANDLE_RISK_PCT_FOR_GATE = 0.20
PAPER_CANDLE_MIN_RR = 2.0
PAPER_CANDLE_MAX_WAIT_BARS = 2

_ENGINE = CandlestickEngine(
    EngineConfig(
        risk_pct=PAPER_CANDLE_RISK_PCT_FOR_GATE,
        min_rr=PAPER_CANDLE_MIN_RR,
        max_wait_bars=PAPER_CANDLE_MAX_WAIT_BARS,
    )
)
_TOKEN_CACHE: dict[tuple[str, str], int] = {}
_CONFIRMED_PLANS: dict[str, dict] = {}


def _rejected(quantity: int, reason: str) -> dict:
    return {
        "success": False,
        "order_id": None,
        "operation_id": None,
        "status": "REJECTED",
        "reason": reason,
        "requested_quantity": int(quantity),
        "filled_quantity": 0,
        "average_price": None,
        "entry_confirmation_pending": False,
        "resolved": True,
    }


def cash_quantity(capital: float, entry_price: float, allocation_pct: float = 100.0) -> int:
    """Whole-share cash-only fallback quantity; never exceeds allocation."""
    try:
        capital = float(capital)
        entry_price = float(entry_price)
        allocation_pct = float(allocation_pct)
    except (TypeError, ValueError):
        return 0
    if not all(math.isfinite(v) for v in (capital, entry_price, allocation_pct)):
        return 0
    if capital <= 0 or entry_price <= 0 or allocation_pct <= 0:
        return 0
    budget = capital * min(allocation_pct, 100.0) / 100.0
    return max(int(math.floor(budget / entry_price)), 0)


def margin_quantity(
    kite,
    symbol: str,
    direction: str,
    exchange: str,
    entry_price: float,
    cfg_obj=cfg,
) -> tuple[int, str, Optional[float]]:
    """Return qty using up to 100% configured PAPER capital.

    Kite order_margins(quantity=1) is used only as a read-only calculator for
    symbol-specific MIS margin.  If that lookup is unavailable/invalid, sizing
    falls closed to cash-only quantity using cfg.CAPITAL; it never invents a
    leverage multiple and never reads live account balance to enlarge PAPER
    capital.
    """
    capital = float(getattr(cfg_obj, "CAPITAL", 0.0) or 0.0)
    allocation = float(
        getattr(cfg_obj, "PAPER_CAPITAL_ALLOCATION_PCT", PAPER_CAPITAL_ALLOCATION_PCT)
        or PAPER_CAPITAL_ALLOCATION_PCT
    )
    cash_qty = cash_quantity(capital, entry_price, allocation)
    budget = capital * min(max(allocation, 0.0), 100.0) / 100.0
    if budget <= 0:
        return 0, "NO_PAPER_CAPITAL", None

    try:
        tx = (
            kite.TRANSACTION_TYPE_BUY
            if str(direction).upper() == "BUY"
            else kite.TRANSACTION_TYPE_SELL
        )
        params = [{
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": tx,
            "variety": cfg_obj.VARIETY,
            "product": cfg_obj.PRODUCT,
            "order_type": cfg_obj.ORDER_TYPE_ENTRY,
            "quantity": 1,
            "price": 0,
            "trigger_price": 0,
        }]
        result = kite.order_margins(params)
        per_share = float(result[0].get("total"))
        if not math.isfinite(per_share) or per_share <= 0:
            raise ValueError("invalid per-share margin")
        qty = max(int(math.floor(budget / per_share)), 0)
        return qty, "KITE_SYMBOL_MARGIN", per_share
    except Exception as exc:
        logger.warning(
            "%s: PAPER symbol margin unavailable (%s); using cash-only sizing",
            symbol,
            exc,
        )
        return cash_qty, "CASH_ONLY_FALLBACK", None


def _native_5m_frame(trading_main, kite, symbol: str, exchange: str):
    key = (exchange, symbol)
    token = _TOKEN_CACHE.get(key)
    if token is None:
        token = trading_main.get_instrument_token(kite, symbol, exchange)
        _TOKEN_CACHE[key] = token
    return trading_main.fetch_candles(
        kite,
        token,
        PAPER_CANDLE_INTERVAL,
        lookback_days=5,
        trim_incomplete=True,
    )


def _plan_payload(plan, frame) -> dict:
    entry_time = None
    if frame is not None and not frame.empty and 0 <= int(plan.entry_index) < len(frame):
        try:
            entry_time = frame.iloc[int(plan.entry_index)]["date"]
        except Exception:
            entry_time = None
    return {
        "plan": plan,
        "entry_time": entry_time,
    }


def install(trading_main) -> None:
    """Install the PAPER-only 5m final gate into an already-imported main.py."""
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: native-5m full-capital gate is PAPER only")

    # The legacy PAPER launcher deliberately monkey-patches PA to observational
    # and disables MA.  This experiment explicitly requires both as hard gates.
    # Reloading restores the real implementation, then updates main.py's local
    # alias (main imports evaluate_price_action by name).
    import price_action
    price_action = importlib.reload(price_action)
    trading_main.evaluate_price_action = price_action.evaluate_price_action
    cfg.ENABLE_PRICE_ACTION = True
    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = True

    # 100% of configured PAPER capital may back one position.  One-open-position
    # is required so two simultaneous trades cannot each claim the same 100%.
    cfg.PAPER_CAPITAL_ALLOCATION_PCT = PAPER_CAPITAL_ALLOCATION_PCT
    cfg.PAPER_CANDLESTICK_INTERVAL = PAPER_CANDLE_INTERVAL
    cfg.PAPER_CANDLESTICK_MIN_RR = PAPER_CANDLE_MIN_RR
    cfg.PAPER_CANDLESTICK_MAX_WAIT_BARS = PAPER_CANDLE_MAX_WAIT_BARS
    cfg.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS

    original_place = trading_main.place_entry_order
    if not getattr(original_place, "_native_5m_full_capital_wrapped", False):
        def gated_place_entry_order(
            kite,
            symbol: str,
            direction: str,
            quantity: int,
            exchange: str,
            cfg_obj,
            entry_plan=None,
        ):
            if not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
                return original_place(
                    kite, symbol, direction, quantity, exchange, cfg_obj,
                    entry_plan=entry_plan,
                )

            try:
                frame = _native_5m_frame(trading_main, kite, symbol, exchange)
                tick = trading_main.get_cached_instrument_tick_size(symbol, exchange)
                gate = evaluate_trade_entry(
                    symbol,
                    frame,
                    direction,
                    float(getattr(cfg_obj, "CAPITAL", 0.0) or 0.0),
                    float(tick),
                    _ENGINE,
                )
            except Exception as exc:
                logger.exception("%s: native 5m candlestick gate failed closed", symbol)
                return _rejected(quantity, f"PAPER_5M_CANDLE_GATE_ERROR:{exc}")

            if gate.state == GateState.WAITING:
                logger.info(
                    "%s: PAPER 5m CANDLE WAIT | pattern=%s direction=%s reason=%s",
                    symbol,
                    gate.pattern.value if gate.pattern else None,
                    direction,
                    gate.reason,
                )
                return _rejected(quantity, "PAPER_5M_CANDLE_WAITING")

            if gate.state != GateState.CONFIRMED or gate.plan is None:
                logger.info(
                    "%s: PAPER 5m CANDLE BLOCK | direction=%s reason=%s",
                    symbol,
                    direction,
                    gate.reason,
                )
                return _rejected(quantity, "PAPER_5M_CANDLE_NO_CONFIRMED_PATTERN")

            plan = gate.plan
            if plan.side.value != str(direction).upper():
                return _rejected(quantity, "PAPER_5M_CANDLE_DIRECTION_MISMATCH")
            if float(plan.rr) < PAPER_CANDLE_MIN_RR:
                return _rejected(quantity, "PAPER_5M_CANDLE_RR_BELOW_2R")

            actual_qty, sizing_source, margin_per_share = margin_quantity(
                kite,
                symbol,
                direction,
                exchange,
                plan.entry_price,
                cfg_obj,
            )
            if actual_qty <= 0:
                return _rejected(actual_qty, "PAPER_5M_FULL_CAPITAL_QTY_ZERO")

            payload = dict(entry_plan or {})
            payload.update({
                "signal_entry_price": float(plan.entry_price),
                "signal_stop_price": float(plan.stop_price),
                "signal_target_price": float(plan.target_price),
                # The geometric candlestick levels are authoritative for this
                # PAPER experiment; do not reconstruct a flat % stop.
                "fixed_target_enabled": False,
                "paper_candlestick_gate": True,
                "paper_candlestick_timeframe": PAPER_CANDLE_INTERVAL,
                "paper_candlestick_pattern": plan.pattern.value,
                "paper_candlestick_trigger": plan.trigger.value,
                "paper_candlestick_rr": float(plan.rr),
                "paper_capital_allocation_pct": float(
                    getattr(cfg_obj, "PAPER_CAPITAL_ALLOCATION_PCT", 100.0)
                ),
                "paper_sizing_source": sizing_source,
                "paper_margin_per_share": margin_per_share,
                "paper_gate_original_quantity": int(quantity),
                "paper_full_capital_quantity": int(actual_qty),
            })
            _CONFIRMED_PLANS[symbol] = _plan_payload(plan, frame)

            logger.warning(
                "%s: PAPER 5m CANDLE CONFIRMED | %s %s | entry=%.4f SL=%.4f TP=%.4f RR=%.2f "
                "| qty=%s sizing=%s margin/share=%s | aggregate-risk guard remains final",
                symbol,
                direction,
                plan.pattern.value,
                plan.entry_price,
                plan.stop_price,
                plan.target_price,
                plan.rr,
                actual_qty,
                sizing_source,
                margin_per_share,
            )
            result = original_place(
                kite,
                symbol,
                direction,
                actual_qty,
                exchange,
                cfg_obj,
                entry_plan=payload,
            )
            if not result.get("success"):
                _CONFIRMED_PLANS.pop(symbol, None)
            return result

        gated_place_entry_order._native_5m_full_capital_wrapped = True
        trading_main.place_entry_order = gated_place_entry_order

    original_build = trading_main.build_confirmed_position
    if not getattr(original_build, "_native_5m_geometric_levels_wrapped", False):
        def build_candlestick_position(
            signal,
            entry_result,
            exchange,
            cfg_obj,
            *,
            tick_size=0.05,
            signal_analytics=None,
        ):
            saved = _CONFIRMED_PLANS.pop(getattr(signal, "symbol", ""), None)
            if not saved or not bool(getattr(cfg_obj, "PAPER_TRADING", False)):
                return original_build(
                    signal,
                    entry_result,
                    exchange,
                    cfg_obj,
                    tick_size=tick_size,
                    signal_analytics=signal_analytics,
                )

            plan = saved["plan"]
            replacement = replace(
                signal,
                entry_price=float(plan.entry_price),
                stop_loss=float(plan.stop_price),
                target=float(plan.target_price),
                timestamp=(saved.get("entry_time") or signal.timestamp),
            )

            # build_confirmed_position normally reconstructs a flat fixed stop
            # when ENABLE_FIXED_TARGET=True.  Temporarily suppress that one
            # reconstruction so this PAPER candlestick position is born with
            # the tested geometric SL and 2R TP.  Single-threaded scan execution
            # makes this scoped temporary switch deterministic.
            old_fixed = bool(getattr(cfg_obj, "ENABLE_FIXED_TARGET", False))
            try:
                cfg_obj.ENABLE_FIXED_TARGET = False
                position = original_build(
                    replacement,
                    entry_result,
                    exchange,
                    cfg_obj,
                    tick_size=tick_size,
                    signal_analytics=signal_analytics,
                )
            finally:
                cfg_obj.ENABLE_FIXED_TARGET = old_fixed

            # The pre-existing PAPER emergency-stop wrapper may have replaced
            # stop after the base constructor.  For this explicitly selected
            # candlestick experiment the geometric pattern stop is authoritative.
            position["paper_pre_candlestick_stop"] = position.get("stop")
            position["paper_strategy_stop"] = float(plan.stop_price)
            position["paper_original_stop"] = float(plan.stop_price)
            position["paper_emergency_stop_active"] = False
            position["stop"] = float(plan.stop_price)
            position["target"] = float(plan.target_price)
            position["hybrid_exit_enabled"] = False
            position["paper_candlestick_gate"] = True
            position["paper_candlestick_timeframe"] = PAPER_CANDLE_INTERVAL
            position["paper_candlestick_pattern"] = plan.pattern.value
            position["paper_candlestick_trigger"] = plan.trigger.value
            position["paper_candlestick_rr"] = float(plan.rr)
            return position

        build_candlestick_position._native_5m_geometric_levels_wrapped = True
        trading_main.build_confirmed_position = build_candlestick_position

    logger.warning(
        "PAPER NATIVE-5m MASTER CANDLE GATE ACTIVE: PA=HARD, MA=HARD, native 5m, "
        "VWAP+EMA50+volume strict, wait<=%s bars, RR>=%.1f, capital allocation<=%.0f%%, max_open=%s; LIVE untouched",
        PAPER_CANDLE_MAX_WAIT_BARS,
        PAPER_CANDLE_MIN_RR,
        PAPER_CAPITAL_ALLOCATION_PCT,
        PAPER_MAX_OPEN_POSITIONS,
    )
