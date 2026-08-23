"""
Process-restart crash recovery (spec #27). On every startup, ALWAYS
asks the broker directly whether a position or pending order already
exists before trusting (or even reading) any local state file --
"program restarted = no position" is exactly the assumption spec #27
forbids.

Pure reconciliation logic lives here; the actual kite.positions()/
kite.orders() calls are the caller's (launcher.py's) responsibility,
passed in as already-fetched data so this stays testable without a
broker connection.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from fno_bot.monitoring.disconnect_handler import reconcile_position_with_broker
from fno_bot.execution import order_store

logger = logging.getLogger("fno.crash_recovery")


@dataclass(frozen=True)
class StartupRecoveryPlan:
    has_unexpected_position: bool
    has_pending_orders: bool
    position_reconciliation: dict
    unresolved_orders: list
    action_summary: str


def compute_startup_recovery_plan(
    *,
    local_position: Optional[dict],
    broker_net_quantity: int,
    local_pending_orders_path: str = None,
) -> StartupRecoveryPlan:
    """
    Pure function: given the locally-saved position (or None) and the
    broker's real net quantity for this tradingsymbol, plus whatever
    unresolved order intents exist in the durable order store, decides
    what recovery is needed before the state machine may proceed past
    BOOT.

    Never assumes: a clean local state means nothing is happening on
    the broker side (checked via broker_net_quantity), NOR that an
    empty order store means no order is outstanding (checked via
    order_store.list_unresolved_orders(), which persists across restarts
    independent of position_store).
    """
    position_reconciliation = reconcile_position_with_broker(local_position, broker_net_quantity)
    unresolved = order_store.list_unresolved_orders(local_pending_orders_path)

    has_unexpected_position = not position_reconciliation["consistent"]
    has_pending_orders = len(unresolved) > 0

    if not has_unexpected_position and not has_pending_orders:
        summary = "Clean startup: local state (or absence of it) matches broker truth, no unresolved orders."
    else:
        parts = []
        if has_unexpected_position:
            parts.append(f"position reconciliation required ({position_reconciliation['action']})")
        if has_pending_orders:
            parts.append(f"{len(unresolved)} unresolved order(s) from a prior session must be reconciled "
                         f"against the broker's order book before any new trading action")
        summary = "RECOVERY REQUIRED: " + "; ".join(parts)

    return StartupRecoveryPlan(
        has_unexpected_position=has_unexpected_position,
        has_pending_orders=has_pending_orders,
        position_reconciliation=position_reconciliation,
        unresolved_orders=unresolved,
        action_summary=summary,
    )


def requires_recovery_state(plan: StartupRecoveryPlan) -> bool:
    """True if BOOT must transition to a dedicated RECOVERY state
    before PREPARE, rather than proceeding straight into a normal
    session start."""
    return plan.has_unexpected_position or plan.has_pending_orders
