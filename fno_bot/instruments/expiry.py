"""
Correct-expiry resolution, driven entirely by the real contract master
-- never by a manually computed calendar rule (spec #29: "never assume
expiry dates manually").

Weekly-vs-monthly expiry differences, exchange holidays, and expiry-day
quirks are all implicitly handled correctly by this approach: whatever
expiries the broker's own instrument dump lists for today ARE the
valid tradeable expiries, full stop. There is no separate holiday
calendar or date-arithmetic logic to get wrong here.
"""
import logging
from datetime import date
from typing import Optional

from fno_bot.instruments.contract_master import ContractRecord

logger = logging.getLogger("fno.expiry")


def available_expiries(records: list[ContractRecord]) -> list[date]:
    """All distinct expiry dates present in the contract master for
    this underlying, sorted ascending (nearest first)."""
    return sorted({r.expiry for r in records})


def current_expiry(records: list[ContractRecord], as_of: date = None) -> Optional[date]:
    """
    The nearest expiry that is still tradeable as of `as_of` (today by
    default) -- i.e. expiry >= as_of. On the expiry date itself, that
    contract is still the current expiry until it actually expires
    (Kite continues to list and trade it through the session).

    Returns None if no future/current expiry exists in the contract
    master (a genuinely abnormal condition -- e.g. an empty or badly
    stale dump), which the caller must treat as "cannot proceed",
    never as "fall back to some other expiry".
    """
    as_of = as_of or date.today()
    upcoming = [e for e in available_expiries(records) if e >= as_of]
    if not upcoming:
        logger.error(f"No tradeable expiry >= {as_of.isoformat()} found in contract master "
                     f"({len(records)} records) -- contract master may be stale or underlying misconfigured")
        return None
    return upcoming[0]


def expiries_for_underlying_sanity_check(records: list[ContractRecord], underlying_name: str) -> dict:
    """
    Read-only diagnostic used at PREPARE time to log what the bot
    actually sees, so an expiry-selection surprise is visible in the
    audit trail before any decision depends on it, not discovered
    after the fact.
    """
    exp = available_expiries(records)
    return {
        "underlying": underlying_name,
        "available_expiry_count": len(exp),
        "nearest_expiries": [e.isoformat() for e in exp[:3]],
    }
