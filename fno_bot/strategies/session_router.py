"""Time-of-day routing for the PAPER options strategy family."""
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum


class TradingSession(str, Enum):
    WAIT = "WAIT"
    OPENING = "OPENING"
    INTRADAY = "INTRADAY"
    NO_NEW_ENTRIES = "NO_NEW_ENTRIES"
    FORCE_EXIT = "FORCE_EXIT"


@dataclass(frozen=True)
class SessionSchedule:
    opening_start: time = time(9, 15)
    opening_end: time = time(9, 20)
    intraday_end: time = time(14, 45)
    force_exit: time = time(15, 15)


def route_session(now: datetime, schedule: SessionSchedule = SessionSchedule()) -> TradingSession:
    current = now.timetz().replace(tzinfo=None) if now.tzinfo else now.time()
    if current < schedule.opening_start:
        return TradingSession.WAIT
    if current < schedule.opening_end:
        return TradingSession.OPENING
    if current < schedule.intraday_end:
        return TradingSession.INTRADAY
    if current < schedule.force_exit:
        return TradingSession.NO_NEW_ENTRIES
    return TradingSession.FORCE_EXIT

