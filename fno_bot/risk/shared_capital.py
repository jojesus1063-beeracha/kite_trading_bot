"""
Shared-capital coordination between the equity bot and this F&O bot
(architecture review Finding 2): both draw on the SAME broker account's
margin. Without this, two independent processes can each check
`kite.margins()` in the same instant, both see the same free capital,
and both commit against it -- a real overcommitment race.

This is a minimal reservation ledger, same locking discipline as
execution/order_store.py (fcntl.flock on a companion .lock file around
the whole read-modify-write). It does NOT replace either bot's own
margin check against the broker (executor.cap_quantity_by_margin()
equivalent) -- it's an ADDITIONAL, cooperative layer: a bot reserves
an amount before committing to a trade, and releases it when the
trade closes or is aborted. Fails safe (SHARED_CAPITAL_CHECK_ENABLED
gates it off entirely; if disabled, this bot behaves as if it were
the only consumer of margin, per config.py's documented default).

IMPORTANT CAVEAT: this is only YOUR proposed mechanism, not yet
adopted by the equity bot -- the equity bot does not currently write
to this ledger at all. Until the equity bot is also updated to
reserve/release through this same file, this ledger only prevents
the F&O bot from overcommitting against ITS OWN reservations; it
cannot see or account for what the equity bot is doing. Treat this as
the F&O side of a mechanism that needs sign-off and a matching change
on the equity side before it delivers the full cross-bot protection
described in the architecture review.
"""
import json
import os
import uuid
import fcntl
import logging
import contextlib
from datetime import datetime

logger = logging.getLogger("fno.shared_capital")


class SharedCapitalError(Exception):
    pass


@contextlib.contextmanager
def _file_lock(path):
    lock_path = path + ".lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load(path) -> dict:
    if not os.path.exists(path):
        return {"reservations": []}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        raise SharedCapitalError(f"shared capital ledger at {path} is corrupt -- refusing to guess its state")
    if "reservations" not in data:
        raise SharedCapitalError(f"shared capital ledger at {path} has an unexpected structure")
    return data


def _save(data: dict, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def total_reserved(path: str, exclude_bot: str = None) -> float:
    """Sum of all currently-active reservations, optionally excluding
    one bot's own (e.g. to compute 'how much is OTHER bots holding')."""
    data = _load(path)
    return sum(
        r["amount"] for r in data["reservations"]
        if r.get("bot") != exclude_bot
    )


def reserve(path: str, bot: str, amount: float, note: str = "") -> str:
    """Reserves `amount` rupees against the shared pool, returns a
    reservation_id to release later. Raises nothing on 'insufficient
    capital' -- this ledger tracks intent, not a hard cap; the actual
    hard cap is still each bot's own broker margin check. This just
    makes the OTHER bot's reservations visible before that check runs."""
    with _file_lock(path):
        data = _load(path)
        reservation_id = str(uuid.uuid4())
        data["reservations"].append({
            "reservation_id": reservation_id, "bot": bot, "amount": amount,
            "note": note, "created_at": datetime.now().isoformat(),
        })
        _save(data, path)
        return reservation_id


def release(path: str, reservation_id: str):
    with _file_lock(path):
        data = _load(path)
        before = len(data["reservations"])
        data["reservations"] = [r for r in data["reservations"] if r["reservation_id"] != reservation_id]
        if len(data["reservations"]) == before:
            logger.warning(f"release() called for unknown reservation_id={reservation_id} -- no-op")
            return
        _save(data, path)
