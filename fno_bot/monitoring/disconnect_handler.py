"""
WebSocket disconnect handling (spec #26). Broker position/order state
is the source of truth after any uncertain network event -- this
module never assumes the WebSocket's last-known state is still
accurate once a disconnect has happened.

Pure decision function (`decide_disconnect_action`) separated from the
thin orchestration that actually calls the broker/ticker, so the
"what should happen" logic is fully unit-testable without a live
connection.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("fno.disconnect_handler")


class DisconnectAction(str, Enum):
    OK = "OK"                                        # connected, nothing to do
    STOP_NEW_ENTRIES = "STOP_NEW_ENTRIES"             # disconnected, flat -- block entries, keep retrying connect
    ATTEMPT_RECONNECT = "ATTEMPT_RECONNECT"            # disconnected, position open, still within recovery window
    RECONCILE_WITH_BROKER = "RECONCILE_WITH_BROKER"    # just reconnected -- must re-verify state before resuming
    EMERGENCY_POSITION_HANDLING = "EMERGENCY_POSITION_HANDLING"  # position open, disconnected beyond timeout


@dataclass(frozen=True)
class DisconnectDecision:
    action: DisconnectAction
    reason: str


def decide_disconnect_action(
    *,
    is_connected: bool,
    just_reconnected: bool,
    has_open_position: bool,
    disconnected_seconds: Optional[float],
    recovery_timeout_seconds: float,
) -> DisconnectDecision:
    """
    `just_reconnected`: True only on the FIRST evaluation after a
    connection is restored -- forces one mandatory broker reconciliation
    pass before normal monitoring resumes, per spec #26 step 3
    ("restore monitoring" only happens after reconciliation, never
    automatically just because the socket is back up).
    """
    if just_reconnected:
        return DisconnectDecision(DisconnectAction.RECONCILE_WITH_BROKER,
                                    "connection restored -- must reconcile against broker before resuming")

    if is_connected:
        return DisconnectDecision(DisconnectAction.OK, "connected")

    if not has_open_position:
        return DisconnectDecision(DisconnectAction.STOP_NEW_ENTRIES,
                                    "disconnected while flat -- no new entries until reconnected")

    disconnected_seconds = disconnected_seconds or 0.0
    if disconnected_seconds >= recovery_timeout_seconds:
        return DisconnectDecision(
            DisconnectAction.EMERGENCY_POSITION_HANDLING,
            f"disconnected {disconnected_seconds:.0f}s with an open position, "
            f"exceeding recovery timeout ({recovery_timeout_seconds}s) -- cannot safely wait longer"
        )

    return DisconnectDecision(
        DisconnectAction.ATTEMPT_RECONNECT,
        f"disconnected {disconnected_seconds:.0f}s with an open position, "
        f"still within recovery timeout ({recovery_timeout_seconds}s)"
    )


def reconcile_position_with_broker(local_position: Optional[dict], broker_net_quantity: int) -> dict:
    """
    Compares the locally-remembered position against the broker's
    actual net quantity for this tradingsymbol (source of truth after
    any disconnect/restart -- spec #26/#27). Never trusts the local
    record over the broker's.

    Returns a dict describing the discrepancy (if any) and what the
    caller should do -- this function makes no broker calls and takes
    no action itself, keeping it pure and testable.
    """
    local_quantity = local_position.get("quantity", 0) if local_position else 0

    if local_quantity == broker_net_quantity:
        return {"consistent": True, "action": "NONE",
                "detail": f"local and broker agree on quantity={broker_net_quantity}"}

    if local_quantity == 0 and broker_net_quantity != 0:
        return {"consistent": False, "action": "RECOVER_UNEXPECTED_POSITION",
                "detail": f"broker shows quantity={broker_net_quantity} but local state had none -- "
                          f"entering recovery to reconstruct POSITION_OPEN from broker truth"}

    if local_quantity != 0 and broker_net_quantity == 0:
        return {"consistent": False, "action": "CLEAR_STALE_LOCAL_POSITION",
                "detail": f"local state shows quantity={local_quantity} but broker has none -- "
                          f"position must have already closed (e.g. exit confirmed after a crash); "
                          f"clearing local state, no further action on the broker side"}

    return {"consistent": False, "action": "RECOVER_QUANTITY_MISMATCH",
            "detail": f"local quantity={local_quantity} != broker quantity={broker_net_quantity} -- "
                      f"broker is authoritative, reconstructing local state to match"}
