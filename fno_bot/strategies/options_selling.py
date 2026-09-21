"""Rules specific to the paper options-selling strategy.

The existing premium-rotation signal says which underlying direction is
confirmed. Selling uses the opposite option leg: bullish underlying ->
sell PE; bearish underlying -> sell CE. This module contains no broker
or order calls.
"""
from dataclasses import dataclass

BULLISH = "BULLISH_ROTATION"
BEARISH = "BEARISH_ROTATION"

@dataclass(frozen=True)
class SellDecision:
    underlying_direction: str
    option_type: str
    side: str = "SELL"
    reason: str = ""

def decide_short_leg(classification: str) -> SellDecision | None:
    if classification == BULLISH:
        return SellDecision(classification, "PE", reason="bullish underlying: sell PE")
    if classification == BEARISH:
        return SellDecision(classification, "CE", reason="bearish underlying: sell CE")
    return None

def short_premium_move(entry_price: float, current_price: float) -> float:
    """Positive means profit for a short option; negative means loss."""
    return entry_price - current_price

def otm_strike(spot: float, atm_strike: float, strike_interval: float, option_type: str, steps: int = 1) -> float:
    """Return a modestly OTM strike. PE is below spot; CE is above spot."""
    if steps < 1:
        raise ValueError("steps must be >= 1")
    if option_type == "PE":
        return atm_strike - strike_interval * steps
    if option_type == "CE":
        return atm_strike + strike_interval * steps
    raise ValueError("option_type must be CE or PE")
