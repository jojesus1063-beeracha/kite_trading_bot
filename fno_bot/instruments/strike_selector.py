"""
Dynamic ATM strike selection (spec #4) -- the strike interval, expiry,
and available contracts all come from real data, never a hardcoded
value like 77200.
"""
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from fno_bot.instruments.contract_master import ContractRecord

logger = logging.getLogger("fno.strike_selector")


@dataclass(frozen=True)
class StrikeSelection:
    underlying_price: float
    strike_interval: int
    atm_strike: float
    expiry: date
    ce_contract: ContractRecord
    pe_contract: ContractRecord


def round_to_strike_interval(price: float, strike_interval: int) -> float:
    """Nearest valid strike for this underlying's exchange-defined
    interval (e.g. 100 for SENSEX, 50 for NIFTY) -- standard round-
    half-up to the nearest interval multiple."""
    if strike_interval <= 0:
        raise ValueError(f"strike_interval must be positive, got {strike_interval}")
    return round(price / strike_interval) * strike_interval


def select_atm_contracts(
    records: list[ContractRecord],
    underlying_price: float,
    expiry: date,
    strike_interval: int,
) -> Optional[StrikeSelection]:
    """
    Computes the ATM strike from the live underlying price and
    verifies BOTH a CE and a PE contract actually exist for that
    strike + expiry (spec #4: "verify that both CE and PE contracts
    actually exist before proceeding"). Returns None (never guesses
    or falls back to a nearby strike silently) if either leg is
    missing -- the caller must treat that as a reason not to trade,
    not as something to work around.
    """
    atm_strike = round_to_strike_interval(underlying_price, strike_interval)

    candidates = [r for r in records if r.expiry == expiry and r.strike == atm_strike]
    ce = next((r for r in candidates if r.instrument_type == "CE"), None)
    pe = next((r for r in candidates if r.instrument_type == "PE"), None)

    if ce is None or pe is None:
        logger.error(
            f"STRIKE_SELECTION_FAILED: underlying_price={underlying_price} "
            f"atm_strike={atm_strike} expiry={expiry.isoformat()} "
            f"ce_found={ce is not None} pe_found={pe is not None} "
            f"-- refusing to proceed without both legs confirmed to exist"
        )
        return None

    return StrikeSelection(
        underlying_price=underlying_price,
        strike_interval=strike_interval,
        atm_strike=atm_strike,
        expiry=expiry,
        ce_contract=ce,
        pe_contract=pe,
    )
