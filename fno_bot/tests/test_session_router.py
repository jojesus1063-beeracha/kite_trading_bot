from datetime import datetime

from fno_bot.strategies.session_router import TradingSession, route_session


def _at(hour, minute):
    return datetime(2026, 8, 26, hour, minute)


def test_routes_each_strategy_session_at_boundaries():
    assert route_session(_at(9, 14)) == TradingSession.WAIT
    assert route_session(_at(9, 15)) == TradingSession.OPENING
    assert route_session(_at(9, 20)) == TradingSession.INTRADAY
    assert route_session(_at(14, 45)) == TradingSession.NO_NEW_ENTRIES
    assert route_session(_at(15, 15)) == TradingSession.FORCE_EXIT

