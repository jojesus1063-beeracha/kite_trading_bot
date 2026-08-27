#!/usr/bin/env python3
"""
Matmon HaElohim — PAPER entry policy.

Technical entry authorization:
    1. EMA9 / EMA21 determines candidate direction.
    2. DI must agree with that direction.
    3. Live quote repricing must confirm that direction.

Legacy indicators are observational only.

NO LIVE ORDERS.
"""

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class MatmonDecision:
    accepted: bool
    direction: Optional[str]
    reason: str

    ema9: Optional[float] = None
    ema21: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None

    di_agree: bool = False
    quote_confirm: bool = False

    def to_dict(self):
        return asdict(self)


def ema_direction(ema9, ema21):
    if ema9 is None or ema21 is None:
        return None

    ema9 = float(ema9)
    ema21 = float(ema21)

    if ema9 > ema21:
        return "BUY"

    if ema9 < ema21:
        return "SELL"

    return None


def di_agrees(direction, plus_di, minus_di):
    if (
        direction not in {"BUY", "SELL"}
        or plus_di is None
        or minus_di is None
    ):
        return False

    plus_di = float(plus_di)
    minus_di = float(minus_di)

    if direction == "BUY":
        return plus_di > minus_di

    return minus_di > plus_di


def quote_confirms(
    direction,
    first_bid,
    first_ask,
    last_bid,
    last_ask,
):
    """
    Quote confirmation deliberately checks REPRICING,
    not merely visible bid/ask quantity.

    BUY:
        best bid must rise
        AND best ask must rise.

    SELL:
        best bid must fall
        AND best ask must fall.

    This prevents a large stationary bid wall from being
    interpreted automatically as bullish confirmation.
    """

    values = (
        first_bid,
        first_ask,
        last_bid,
        last_ask,
    )

    if any(v is None for v in values):
        return False

    first_bid = float(first_bid)
    first_ask = float(first_ask)
    last_bid = float(last_bid)
    last_ask = float(last_ask)

    if min(values) <= 0:
        return False

    if first_ask < first_bid or last_ask < last_bid:
        return False

    if direction == "BUY":
        return (
            last_bid > first_bid
            and last_ask > first_ask
        )

    if direction == "SELL":
        return (
            last_bid < first_bid
            and last_ask < first_ask
        )

    return False


def evaluate(
    *,
    ema9,
    ema21,
    plus_di,
    minus_di,
    first_bid,
    first_ask,
    last_bid,
    last_ask,
):
    direction = ema_direction(ema9, ema21)

    if direction is None:
        return MatmonDecision(
            False,
            None,
            "EMA_DIRECTION_UNAVAILABLE",
            ema9=ema9,
            ema21=ema21,
            plus_di=plus_di,
            minus_di=minus_di,
        )

    di_ok = di_agrees(
        direction,
        plus_di,
        minus_di,
    )

    if not di_ok:
        return MatmonDecision(
            False,
            direction,
            "DI_DISAGREES",
            ema9=ema9,
            ema21=ema21,
            plus_di=plus_di,
            minus_di=minus_di,
            di_agree=False,
        )

    quote_ok = quote_confirms(
        direction,
        first_bid,
        first_ask,
        last_bid,
        last_ask,
    )

    if not quote_ok:
        return MatmonDecision(
            False,
            direction,
            "QUOTE_NOT_CONFIRMED",
            ema9=ema9,
            ema21=ema21,
            plus_di=plus_di,
            minus_di=minus_di,
            di_agree=True,
            quote_confirm=False,
        )

    return MatmonDecision(
        True,
        direction,
        "MATMON_ENTRY_CONFIRMED",
        ema9=ema9,
        ema21=ema21,
        plus_di=plus_di,
        minus_di=minus_di,
        di_agree=True,
        quote_confirm=True,
    )
