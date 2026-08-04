"""Fail-closed handoff from a confirmed entry fill to broker protection."""

from __future__ import annotations

import math

from protective_stop import place_protective_stop
from trade_levels import fixed_levels_from_fill


SIGNAL_ANALYTICS_FIELDS = {
    "signal_id",
    "candidate_rank",
    "candidate_count",
    "ranking_score",
    "entry_quality_score",
    "entry_quality_detail",
    "entry_context_score",
    "entry_context_detail",
    "confirmation_count",
    "adx_state",
    "adx_current",
    "adx_previous",
    "adx_delta",
    "relative_strength_score",
    "relative_strength_detail",
    "market_trend_reason",
    "sector_trend",
    "sector_trend_reason",
    "signal_candle_start",
    "signal_candle_close",
    "scan_started_at",
    "order_submitted_at",
    "entry_delay_seconds",
}


def _validated_signal_analytics(signal_analytics):
    if not isinstance(signal_analytics, dict):
        return {}

    return {
        key: value
        for key, value in signal_analytics.items()
        if key in SIGNAL_ANALYTICS_FIELDS
    }


def build_entry_plan(
    signal,
    cfg,
    *,
    tick_size=0.05,
    signal_analytics=None,
):
    """Capture the strategy values needed to reconstruct a fill on restart."""

    plan = {
        "signal_entry_price": float(signal.entry_price),
        "signal_stop_price": float(signal.stop_loss),
        "signal_target_price": float(signal.target),
        "signal_timestamp": str(signal.timestamp),
        "fixed_target_enabled": bool(
            getattr(cfg, "ENABLE_FIXED_TARGET", False)
        ),
        "stop_loss_percent": float(
            getattr(cfg, "STOP_LOSS_PERCENT", 0.45)
        ),
        "profit_target_percent": float(
            getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5)
        ),
        "tick_size": float(tick_size),
    }

    if signal_analytics:
        plan["signal_analytics"] = _validated_signal_analytics(
            signal_analytics
        )

    return plan


def build_confirmed_position(
    signal,
    entry_result,
    exchange,
    cfg,
    *,
    tick_size=0.05,
    signal_analytics=None,
):
    """Create local state from broker-confirmed quantity and price only."""

    confirmed_qty = int(entry_result.get("filled_quantity") or 0)

    if confirmed_qty <= 0:
        raise ValueError("A confirmed position requires a positive fill")

    broker_average = entry_result.get("average_price")
    is_paper = bool(getattr(cfg, "PAPER_TRADING", True))

    if broker_average is None:
        confirmed_entry_price = float(signal.entry_price)
    else:
        confirmed_entry_price = float(broker_average)

    if not math.isfinite(confirmed_entry_price) or confirmed_entry_price <= 0:
        raise ValueError("Confirmed entry price must be positive and finite")

    stop_price = float(signal.stop_loss)
    target_price = float(signal.target)

    if getattr(cfg, "ENABLE_FIXED_TARGET", False):
        stop_price, target_price = fixed_levels_from_fill(
            signal.direction,
            confirmed_entry_price,
            getattr(cfg, "STOP_LOSS_PERCENT", 0.45),
            getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
        )

    protection_state = "PAPER" if is_paper else "PENDING"
    missing_live_average = not is_paper and broker_average is None

    position = {
        "direction": signal.direction,
        "qty": confirmed_qty,
        "entry": confirmed_entry_price,
        "stop": stop_price,
        "target": target_price,
        "exchange": exchange,
        "peak_price": confirmed_entry_price,
        "tight_mode": False,
        "entry_time": str(signal.timestamp),
        "entry_order_id": entry_result.get("order_id"),
        "entry_operation_id": entry_result.get("operation_id"),
        "entry_client_tag": entry_result.get("client_tag"),
        "requested_quantity": entry_result.get(
            "requested_quantity",
            confirmed_qty,
        ),
        "filled_quantity": confirmed_qty,
        "entry_fill_status": entry_result.get("status"),
        "entry_average_price": broker_average,
        "entry_confirmation_pending": entry_result.get(
            "entry_confirmation_pending",
            False,
        ),
        "entry_status_message": entry_result.get("reason"),
        "tick_size": float(tick_size),
        "protective_stop_state": (
            "ENTRY_PRICE_UNRESOLVED"
            if missing_live_average
            else protection_state
        ),
        "protective_stop_active": False,
        "protective_stop_confirmation_pending": not is_paper,
        "protective_stop_operation_id": None,
        "protective_stop_order_id": None,
        "protective_stop_client_tag": None,
        "protective_stop_quantity": 0,
        "entry_protected": False,
        "automated_exit_blocked": not is_paper,
        "manual_reconciliation_required": missing_live_average,
    }

    if signal_analytics:
        position.update(_validated_signal_analytics(signal_analytics))

    return position


def build_recovered_position(order_record, execution_result, cfg):
    """Reconstruct a locally missing confirmed fill from durable metadata."""

    filled = int(execution_result.filled_quantity)
    average = execution_result.average_price
    metadata = order_record.get("metadata") or {}

    if filled <= 0:
        raise ValueError("Recovered position requires a confirmed fill")

    if average is None or float(average) <= 0:
        stop_price = None
        target_price = None
        entry_price = average
        manual = True
        protection_state = "ENTRY_PRICE_UNRESOLVED"
    else:
        entry_price = float(average)
        fixed = bool(metadata.get("fixed_target_enabled"))

        if fixed:
            stop_price, target_price = fixed_levels_from_fill(
                order_record["side"],
                entry_price,
                metadata.get(
                    "stop_loss_percent",
                    getattr(cfg, "STOP_LOSS_PERCENT", 0.45),
                ),
                metadata.get(
                    "profit_target_percent",
                    getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
                ),
            )
            manual = False
            protection_state = "PENDING"
        else:
            stop_price = metadata.get("signal_stop_price")
            target_price = metadata.get("signal_target_price")
            manual = stop_price is None or target_price is None
            protection_state = (
                "PLAN_UNRESOLVED" if manual else "PENDING"
            )

    position = {
        "direction": order_record["side"],
        "qty": filled,
        "entry": entry_price,
        "stop": stop_price,
        "target": target_price,
        "exchange": order_record["exchange"],
        "peak_price": entry_price,
        "tight_mode": False,
        "entry_time": order_record["created_at"],
        "entry_order_id": order_record.get("order_id"),
        "entry_operation_id": order_record["operation_id"],
        "entry_client_tag": order_record.get("client_tag"),
        "requested_quantity": order_record["requested_quantity"],
        "filled_quantity": filled,
        "entry_fill_status": execution_result.status,
        "entry_average_price": average,
        "entry_confirmation_pending": not execution_result.terminal,
        "entry_status_message": (
            "recovered from durable entry operation after restart"
        ),
        "tick_size": float(metadata.get("tick_size") or 0.05),
        "protective_stop_state": protection_state,
        "protective_stop_active": False,
        "protective_stop_confirmation_pending": not manual,
        "protective_stop_operation_id": None,
        "protective_stop_order_id": None,
        "protective_stop_client_tag": None,
        "protective_stop_quantity": 0,
        "entry_protected": False,
        "automated_exit_blocked": True,
        "manual_reconciliation_required": manual,
    }

    signal_analytics = metadata.get("signal_analytics")

    if isinstance(signal_analytics, dict):
        position.update(_validated_signal_analytics(signal_analytics))

    return position


def apply_protective_stop_result(position, result):
    """Apply a stop result without ever converting uncertainty to success."""

    state = result.get("state") or result.get("status") or "UNKNOWN"
    active = bool(result.get("active"))
    triggered = bool(result.get("triggered"))

    position["protective_stop_state"] = state
    position["protective_stop_active"] = active
    position["protective_stop_confirmation_pending"] = bool(
        result.get("confirmation_pending")
    )
    position["protective_stop_operation_id"] = result.get(
        "operation_id"
    )
    position["protective_stop_order_id"] = result.get("order_id")
    position["protective_stop_client_tag"] = result.get("client_tag")
    position["protective_stop_status_message"] = result.get("reason")
    stop_quantity = int(result.get("requested_quantity") or 0)
    position["protective_stop_quantity"] = (
        stop_quantity if active else 0
    )

    if result.get("trigger_price") is not None:
        position["protective_stop_trigger_price"] = float(
            result["trigger_price"]
        )
        position["stop"] = float(result["trigger_price"])

    position_quantity = int(
        position.get("filled_quantity", position.get("qty", 0))
        or 0
    )
    coverage_complete = active and stop_quantity == position_quantity
    position["entry_protected"] = coverage_complete

    # A fully active stop may now participate in the coordinated exit
    # state machine. Any uncertainty or coverage mismatch remains blocked.
    position["automated_exit_blocked"] = not coverage_complete

    needs_manual = not coverage_complete

    if active and not coverage_complete:
        position["protective_stop_state"] = (
            "PROTECTION_QUANTITY_MISMATCH"
        )
        position["protective_stop_status_message"] = (
            f"active stop covers {stop_quantity} of "
            f"{position_quantity} confirmed shares"
        )

    if triggered:
        needs_manual = True
        position["automated_exit_blocked"] = True

    position["manual_reconciliation_required"] = needs_manual
    return position


def needs_initial_stop_recovery(position, *, has_store_record):
    """True only for the pre-intent crash window, never uncertainty."""

    return (
        position.get("protective_stop_state") == "PENDING"
        and not position.get("protective_stop_operation_id")
        and not has_store_record
        and not position.get("manual_reconciliation_required")
    )


def protect_confirmed_position(
    kite,
    symbol,
    position,
    cfg,
    *,
    store_path=None,
    stop_placer=place_protective_stop,
):
    """Place exactly one stop for the currently confirmed live quantity."""

    if getattr(cfg, "PAPER_TRADING", True):
        position["protective_stop_state"] = "PAPER"
        position["protective_stop_confirmation_pending"] = False
        position["automated_exit_blocked"] = False
        position["manual_reconciliation_required"] = False
        return {
            "success": True,
            "paper": True,
            "active": False,
            "state": "PAPER",
        }

    if position.get("protective_stop_operation_id"):
        raise RuntimeError(
            "Protective-stop operation already exists; recovery is required"
        )

    entry_price = position.get("entry_average_price")
    stop_price = position.get("stop")

    if entry_price is None or stop_price is None:
        position["protective_stop_state"] = "PLAN_UNRESOLVED"
        position["manual_reconciliation_required"] = True
        position["automated_exit_blocked"] = True
        return {
            "success": False,
            "active": False,
            "confirmation_pending": True,
            "state": "PLAN_UNRESOLVED",
            "reason": "confirmed fill price or stop price is unavailable",
        }

    entry_price = float(entry_price)
    stop_price = float(stop_price)

    if (
        not math.isfinite(entry_price)
        or not math.isfinite(stop_price)
        or entry_price <= 0
        or stop_price <= 0
        or (
            position["direction"] == "BUY"
            and stop_price >= entry_price
        )
        or (
            position["direction"] == "SELL"
            and stop_price <= entry_price
        )
    ):
        position["protective_stop_state"] = "INVALID_PROTECTION_PLAN"
        position["manual_reconciliation_required"] = True
        position["automated_exit_blocked"] = True
        return {
            "success": False,
            "active": False,
            "confirmation_pending": True,
            "state": "INVALID_PROTECTION_PLAN",
            "reason": "stop price is invalid for the confirmed position",
        }

    stop_loss_percent = abs(entry_price - stop_price) / entry_price * 100

    try:
        result = stop_placer(
            kite,
            symbol=symbol,
            position_direction=position["direction"],
            quantity=int(position["filled_quantity"]),
            exchange=position["exchange"],
            confirmed_entry_price=entry_price,
            stop_loss_percent=stop_loss_percent,
            tick_size=float(position.get("tick_size") or 0.05),
            cfg=cfg,
            entry_operation_id=position.get("entry_operation_id"),
            store_path=store_path,
        )
    except Exception as exc:
        result = {
            "success": False,
            "active": False,
            "triggered": False,
            "confirmation_pending": True,
            "status": "PROTECTIVE_STOP_ERROR",
            "state": "PROTECTIVE_STOP_ERROR",
            "reason": str(exc),
            "operation_id": None,
            "order_id": None,
            "client_tag": None,
            "trigger_price": stop_price,
            "requested_quantity": int(position["filled_quantity"]),
        }

    apply_protective_stop_result(position, result)
    return result
