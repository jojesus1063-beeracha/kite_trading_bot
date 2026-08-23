"""Frozen market-regime direction policy.

Pure functions only: no broker, filesystem, network, or logging side effects.
"""

from dataclasses import dataclass
from typing import Optional


_MARKET_ALIASES = {
    "UP": "BULLISH",
    "BULLISH": "BULLISH",
    "DOWN": "BEARISH",
    "BEARISH": "BEARISH",
    "FLAT": "SIDEWAYS",
    "NEUTRAL": "SIDEWAYS",
    "SIDEWAYS": "SIDEWAYS",
}


@dataclass(frozen=True)
class DirectionResolution:
    decision: str
    direction: Optional[str]
    reason: str
    market: Optional[str]
    original_direction: str

    def as_dict(self):
        return {
            "decision": self.decision,
            "direction": self.direction,
            "reason": self.reason,
            "market": self.market,
            "original_direction": self.original_direction,
        }


def normalize_market_trend(market_trend):
    if market_trend is None:
        return None
    return _MARKET_ALIASES.get(str(market_trend).strip().upper())


def resolve_market_direction(market_trend, original_direction):
    """Resolve exactly the frozen six-case policy; invalid input skips."""
    original = str(original_direction).strip().upper()
    market = normalize_market_trend(market_trend)
    if original not in {"BUY", "SELL"} or market is None:
        return DirectionResolution(
            "SKIP", None, "UNKNOWN_MARKET_REGIME", market, original
        ).as_dict()

    table = {
        ("BEARISH", "BUY"): ("NORMAL", "BUY", "BEARISH_BUY_NORMAL"),
        ("BEARISH", "SELL"): (
            "REVERSE", "BUY", "BEARISH_SELL_REVERSE_TO_BUY"
        ),
        ("BULLISH", "BUY"): ("NORMAL", "BUY", "BULLISH_BUY_NORMAL"),
        ("BULLISH", "SELL"): ("NORMAL", "SELL", "BULLISH_SELL_NORMAL"),
        ("SIDEWAYS", "BUY"): (
            "REVERSE", "SELL", "SIDEWAYS_BUY_REVERSE_TO_SELL"
        ),
        ("SIDEWAYS", "SELL"): ("NORMAL", "SELL", "SIDEWAYS_SELL_NORMAL"),
    }
    decision, direction, reason = table[(market, original)]
    return DirectionResolution(
        decision, direction, reason, market, original
    ).as_dict()
