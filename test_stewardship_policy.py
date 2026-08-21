from types import SimpleNamespace

import pandas as pd

from risk_manager import RiskManager
from stewardship_policy import (
    entry_quality_score,
    preserve_minimum_rr_target,
    two_candle_adverse_confirmation,
)


def _cfg():
    return SimpleNamespace(
        CAPITAL=100000.0,
        MAX_DAILY_LOSS_PCT=0.5,
        RISK_PER_TRADE_PCT=0.2,
        MAX_TRADES_PER_DAY=5,
        MAX_OPEN_POSITIONS=1,
    )


def test_position_sizing_uses_point_two_percent_risk():
    risk = RiskManager(_cfg(), persist=False)
    assert risk.risk_amount_per_trade() == 200.0
    assert risk.position_size(100.0, 98.0) == 100


def test_proposed_trade_rejected_when_total_daily_risk_would_exceed_budget():
    risk = RiskManager(_cfg(), persist=False)
    risk.day.realized_pnl = -250.0
    open_positions = {
        "ABC": {"direction": "BUY", "qty": 50, "entry": 100.0, "stop": 98.0}
    }
    # Used: 250 realized + 100 open stop risk. Proposed 200 => 550 > 500.
    assert risk.total_risk_if_added(open_positions, 200.0) == 550.0
    assert not risk.can_afford_trade(open_positions, 200.0)


def test_unknown_open_stop_fails_safe():
    risk = RiskManager(_cfg(), persist=False)
    open_positions = {
        "ABC": {"direction": "BUY", "qty": 50, "entry": 100.0, "stop": None}
    }
    assert not risk.can_afford_trade(open_positions, 1.0)


def test_fixed_target_can_never_weaken_minimum_rr_buy():
    # Signal target is 2R at 110. Fixed 1.5% target would be 101.5.
    assert preserve_minimum_rr_target("BUY", 100.0, 110.0, 1.5) == 110.0


def test_fixed_target_can_never_weaken_minimum_rr_sell():
    # Signal target is 2R at 90. Fixed 1.5% target would be 98.5.
    assert preserve_minimum_rr_target("SELL", 100.0, 90.0, 1.5) == 90.0


def test_quality_score_consolidates_existing_evidence():
    assert entry_quality_score("HIGH", 5, "ALIGNED") == 85.0
    assert entry_quality_score("MEDIUM", 0, "STRONG_MISALIGNMENT") == 30.0


def test_missing_confidence_is_not_treated_as_good_confidence():
    # Trial logs on Aug 20/21 contained technical_confidence=None.
    # Unknown evidence starts at 40, so price action must genuinely lift it.
    assert entry_quality_score(None, 0, "UNKNOWN") == 40.0
    assert entry_quality_score(None, 25, "UNKNOWN") == 65.0


def test_two_completed_adverse_buy_candles_confirm_exit():
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-08-21 10:00", periods=4, freq="5min"),
            "close": [100.5, 99.5, 98.8, 98.6],
            "ema_entry": [100.0, 100.0, 99.8, 99.5],
        }
    )
    # Last row is forming and ignored. Completed 99.5 -> 98.8 are both
    # below entry/EMA and non-improving.
    assert two_candle_adverse_confirmation(df, "BUY", 100.0, confirm_candles=2)


def test_one_bad_candle_does_not_force_exit():
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-08-21 10:00", periods=4, freq="5min"),
            "close": [100.5, 99.5, 100.2, 99.0],
            "ema_entry": [100.0, 100.0, 100.0, 100.0],
        }
    )
    assert not two_candle_adverse_confirmation(df, "BUY", 100.0, confirm_candles=2)
