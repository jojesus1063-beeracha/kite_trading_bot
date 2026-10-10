#!/usr/bin/env python3
"""Independent, idempotent Matmon MIS end-of-day square-off supervisor."""

from __future__ import annotations

import fcntl
import json
import logging
import shutil
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from position_store import POSITIONS_PATH, clear_positions, load_positions


IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "matmon" / "eod_squareoff"
LOCK = RUNTIME / "squareoff.lock"
TAG = "matmon_eod"
FINAL_DEADLINE = clock_time(15, 10)
ACTIVE_ORDER_STATES = {
    "OPEN", "OPEN PENDING", "TRIGGER PENDING", "VALIDATION PENDING",
    "PUT ORDER REQ RECEIVED", "MODIFY VALIDATION PENDING",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s matmon_eod_squareoff: %(message)s",
)
log = logging.getLogger("matmon_eod_squareoff")


def now_ist():
    return datetime.now(IST)


def audit(event, **fields):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": now_ist().isoformat(), "event": event, **fields}
    path = RUNTIME / f"{now_ist():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def broker_positions(kite):
    rows = kite.positions().get("day", [])
    return {
        (str(row.get("exchange")), str(row.get("tradingsymbol"))): row
        for row in rows
        if str(row.get("product")) == "MIS" and int(row.get("quantity") or 0) != 0
    }


def tracked_exposure(kite, local_positions):
    live = broker_positions(kite)
    result = {}
    for symbol, local in local_positions.items():
        exchange = str(local.get("exchange") or "NSE")
        row = live.get((exchange, symbol))
        if row and int(row.get("quantity") or 0):
            result[(exchange, symbol)] = row
    return result


def order_is_active(order):
    return str(order.get("status") or "").upper() in ACTIVE_ORDER_STATES


def cancel_protective_order(kite, exchange, symbol, local):
    order_id = local.get("protective_stop_order_id")
    if not order_id:
        return

    matching = [
        order for order in kite.orders()
        if str(order.get("order_id")) == str(order_id)
    ]
    if not matching or not order_is_active(matching[-1]):
        return

    variety = matching[-1].get("variety") or "regular"
    log.info("Cancelling protective stop %s for %s:%s", order_id, exchange, symbol)
    audit("PROTECTIVE_CANCEL_REQUEST", exchange=exchange, symbol=symbol, order_id=order_id)
    kite.cancel_order(variety=variety, order_id=order_id)

    for _ in range(20):
        time.sleep(0.25)
        latest = [
            order for order in kite.orders()
            if str(order.get("order_id")) == str(order_id)
        ]
        if not latest or not order_is_active(latest[-1]):
            audit(
                "PROTECTIVE_CANCEL_TERMINAL",
                exchange=exchange,
                symbol=symbol,
                order_id=order_id,
                status=(latest[-1].get("status") if latest else "NOT_FOUND"),
            )
            return
    raise RuntimeError(f"protective stop {order_id} did not reach a terminal state")


def matching_active_exit(kite, exchange, symbol, side):
    for order in reversed(kite.orders()):
        if (
            str(order.get("exchange")) == exchange
            and str(order.get("tradingsymbol")) == symbol
            and str(order.get("product")) == "MIS"
            and str(order.get("transaction_type")) == side
            and order_is_active(order)
        ):
            return order
    return None


def place_exit(kite, exchange, symbol, quantity):
    side = "SELL" if quantity > 0 else "BUY"
    qty = abs(int(quantity))

    existing = matching_active_exit(kite, exchange, symbol, side)
    if existing:
        log.warning(
            "Existing active exit retained for %s:%s order=%s",
            exchange, symbol, existing.get("order_id"),
        )
        audit(
            "EXIT_ALREADY_ACTIVE", exchange=exchange, symbol=symbol,
            side=side, quantity=qty, order_id=existing.get("order_id"),
        )
        return existing.get("order_id")

    log.warning("Submitting EOD MARKET exit %s %s %s:%s", side, qty, exchange, symbol)
    audit("EXIT_SUBMIT", exchange=exchange, symbol=symbol, side=side, quantity=qty)
    order_id = kite.place_order(
        variety="regular",
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=side,
        quantity=qty,
        product="MIS",
        order_type="MARKET",
        tag=TAG,
    )
    audit(
        "EXIT_ACCEPTED", exchange=exchange, symbol=symbol,
        side=side, quantity=qty, order_id=order_id,
    )
    return order_id


def archive_and_clear_local_state():
    path = Path(POSITIONS_PATH)
    if path.exists():
        RUNTIME.mkdir(parents=True, exist_ok=True)
        backup = RUNTIME / f"open_positions.closed.{now_ist():%Y%m%d_%H%M%S}.json"
        shutil.copy2(path, backup)
        clear_positions()
        audit("LOCAL_STATE_CLEARED", backup=str(backup))


def run(kite=None, *, sleep=time.sleep):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("Another EOD square-off supervisor already holds the lock")
            return 0

        local_positions = load_positions()
        if not local_positions:
            log.info("No Matmon positions are persisted; nothing to square off")
            audit("NO_LOCAL_POSITIONS")
            return 0

        if kite is None:
            from auth import get_kite_client
            kite = get_kite_client()
        audit("SUPERVISOR_START", tracked_symbols=sorted(local_positions))

        last_error = None
        while now_ist().time() < FINAL_DEADLINE:
            try:
                exposure = tracked_exposure(kite, local_positions)
                if not exposure:
                    log.info("Broker confirms zero open Matmon MIS positions")
                    archive_and_clear_local_state()
                    audit("SQUAREOFF_COMPLETE")
                    return 0

                for (exchange, symbol), row in exposure.items():
                    local = local_positions.get(symbol, {})
                    cancel_protective_order(kite, exchange, symbol, local)

                    # A protective stop may fill while its cancellation is processed.
                    refreshed = tracked_exposure(kite, local_positions).get((exchange, symbol))
                    if not refreshed:
                        audit("CLOSED_DURING_PROTECTIVE_CANCEL", exchange=exchange, symbol=symbol)
                        continue
                    place_exit(kite, exchange, symbol, int(refreshed.get("quantity") or 0))

                sleep(2.0)
                last_error = None
            except Exception as exc:
                last_error = repr(exc)
                log.exception("EOD square-off pass failed; broker state will be reconciled before retry")
                audit("PASS_ERROR", error=last_error)
                sleep(2.0)

        remaining = tracked_exposure(kite, local_positions)
        if remaining:
            details = {
                f"{exchange}:{symbol}": int(row.get("quantity") or 0)
                for (exchange, symbol), row in remaining.items()
            }
            log.critical("EOD SQUARE-OFF FAILED before 15:10: %s", details)
            audit("SQUAREOFF_FAILED", remaining=details, last_error=last_error)
            return 2

        archive_and_clear_local_state()
        audit("SQUAREOFF_COMPLETE_AT_DEADLINE")
        return 0


if __name__ == "__main__":
    raise SystemExit(run())
