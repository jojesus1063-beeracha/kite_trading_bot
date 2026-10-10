"""Shared Matmon live acknowledgement and read-only preflight checks."""
import os
import json
from datetime import datetime
from pathlib import Path
LIVE_ACK_ENV = "KITE_LIVE_COMBINED_ACK"
LIVE_ACK_VALUE = "I_ACCEPT_REAL_ORDERS"
TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}

def require_live_acknowledgement() -> None:
    if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
        raise RuntimeError(
            f"SAFETY BLOCK: {LIVE_ACK_ENV} must equal {LIVE_ACK_VALUE}"
        )


def load_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return data


def validate_live_config(data: dict) -> None:
    if data.get("paper_trading") is not False:
        raise RuntimeError("SAFETY BLOCK: user_config paper_trading must be false")


def validate_local_flat(project: Path) -> None:
    positions_path = project / "open_positions.json"
    if positions_path.exists():
        state = load_json_object(positions_path)
        if (
            state.get("positions") or {}
        ):
            raise RuntimeError("Local open_positions.json contains today's exposure")

    pending_path = project / "pending_orders.json"
    if pending_path.exists():
        pending = load_json_object(pending_path)
        unresolved = [row for row in pending.get("orders", []) if not row.get("resolved")]
        if unresolved:
            raise RuntimeError(
                f"Local pending_orders.json has {len(unresolved)} unresolved operation(s)"
            )

    stops_path = project / "protective_stops.json"
    if stops_path.exists():
        stops = load_json_object(stops_path)
        unresolved = [row for row in stops.get("stops", []) if not row.get("resolved")]
        if unresolved:
            raise RuntimeError(
                f"Local protective_stops.json has {len(unresolved)} unresolved stop(s)"
            )


def validate_broker_flat(kite) -> None:
    snapshots = kite.positions()
    if not isinstance(snapshots, dict) or not isinstance(snapshots.get("net"), list) or not isinstance(snapshots.get("day"), list):
        raise RuntimeError("Invalid broker positions response")
    positions = []
    if isinstance(snapshots, dict):
        positions.extend(snapshots.get("net") or [])
        positions.extend(snapshots.get("day") or [])
    active_mis = [
        row
        for row in positions
        if str(row.get("product") or "").upper() == "MIS"
        and int(row.get("quantity") or 0) != 0
    ]
    if active_mis:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_mis})
        raise RuntimeError(f"Broker has active MIS exposure: {symbols}")

    orders = kite.orders()
    if not isinstance(orders, list):
        raise RuntimeError("Invalid broker orders response")
    active_orders = [
        row
        for row in (orders or [])
        if str(row.get("product") or "").upper() == "MIS"
        and str(row.get("status") or "").upper() not in TERMINAL_ORDER_STATUSES
    ]
    if active_orders:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_orders})
        raise RuntimeError(f"Broker has active MIS order(s): {symbols}")
