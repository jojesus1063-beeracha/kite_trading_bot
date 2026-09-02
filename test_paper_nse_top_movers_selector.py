import copy
from datetime import datetime, timedelta
import sys
import types

from zoneinfo import ZoneInfo

fake_auth = types.ModuleType("auth")
fake_auth.get_kite_client = lambda: None
sys.modules.setdefault("auth", fake_auth)
sys.modules.setdefault("requests", types.ModuleType("requests"))

from paper_nse_top_movers_selector import (
    DEFAULT_LOSERS,
    DEFAULT_WINNERS,
    completed_context_metrics,
    movement_rejection_reason,
    select_top_movers,
)


def candidate(symbol, change, turnover=1_000_000):
    return {
        "symbol": symbol,
        "exchange": "NSE",
        "change_pct": change,
        "turnover": turnover,
    }


def test_selects_largest_positive_and_negative_changes():
    rows = [
        candidate("G1", 4.0), candidate("G2", 2.0), candidate("G3", 1.0),
        candidate("L1", -5.0), candidate("L2", -3.0), candidate("L3", -1.0),
    ]
    selected, gainers, losers = select_top_movers(copy.deepcopy(rows), 2, 2)
    assert [item["symbol"] for item in gainers] == ["G1", "G2"]
    assert [item["symbol"] for item in losers] == ["L1", "L2"]
    assert [item["symbol"] for item in selected] == ["G1", "G2", "L1", "L2"]


def test_turnover_breaks_equal_change_ties():
    rows = [candidate("LOW", 2.0, 1_000_000), candidate("HIGH", 2.0, 5_000_000)]
    _, gainers, _ = select_top_movers(copy.deepcopy(rows), 1, 1)
    assert gainers[0]["symbol"] == "HIGH"


def test_zero_change_is_neither_winner_nor_loser():
    selected, gainers, losers = select_top_movers(
        [candidate("FLAT", 0.0)], 10, 10
    )
    assert selected == []
    assert gainers == []
    assert losers == []


def test_mover_group_and_rank_are_persisted():
    rows = [candidate(f"G{i}", 20 - i) for i in range(12)]
    rows += [candidate(f"L{i}", -(20 - i)) for i in range(12)]
    selected, gainers, losers = select_top_movers(copy.deepcopy(rows), 10, 10)
    assert len(selected) == 20
    assert [item["mover_rank"] for item in gainers] == list(range(1, 11))
    assert [item["mover_rank"] for item in losers] == list(range(1, 11))
    assert all(item["mover_group"] == "GAINER" for item in gainers)
    assert all(item["mover_group"] == "LOSER" for item in losers)


def test_default_watchlist_is_25_gainers_and_15_losers():
    rows = [candidate(f"G{i:02d}", 2.0 + i / 100) for i in range(30)]
    rows += [candidate(f"L{i:02d}", -(2.0 + i / 100)) for i in range(20)]
    selected, gainers, losers = select_top_movers(copy.deepcopy(rows))
    assert DEFAULT_WINNERS == 25
    assert DEFAULT_LOSERS == 15
    assert len(selected) == 40
    assert len(gainers) == 25
    assert len(losers) == 15


def test_movement_bounds_hard_reject_only_extreme_candidates():
    settings = {
        "min_abs_change_pct": 1.5,
        "max_abs_change_pct": 8.0,
        "min_day_range_pct": 0.75,
    }
    assert movement_rejection_reason(
        {"change_pct": 1.49, "day_range_pct": 1.0}, **settings
    ) is None
    assert movement_rejection_reason(
        {"change_pct": -8.01, "day_range_pct": 9.0}, **settings
    ) == "absolute_change_above_maximum"
    assert movement_rejection_reason(
        {"change_pct": 2.0, "day_range_pct": 0.74}, **settings
    ) is None
    assert movement_rejection_reason(
        {"change_pct": -2.0, "day_range_pct": 1.0}, **settings
    ) is None


def test_breakout_ready_candidate_ranks_before_larger_raw_mover():
    rows = [
        {
            **candidate("RAW", 7.0, 500_000_000),
            "primary_breakout_ready": False,
            "watchlist_score": 80.0,
            "n20_signed_gap_pct": 0.1,
        },
        {
            **candidate("READY", 2.0, 100_000_000),
            "primary_breakout_ready": True,
            "watchlist_score": 60.0,
            "n20_signed_gap_pct": 0.2,
        },
        candidate("LOSS", -2.0),
    ]
    _, gainers, _ = select_top_movers(copy.deepcopy(rows), 1, 1)
    assert gainers[0]["symbol"] == "READY"
    assert gainers[0]["selection_tier"] == "PRIMARY_BREAKOUT_READY"


def test_completed_context_uses_only_closed_candles_and_exposes_n20_metrics():
    ist = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 8, 13, 14, 0, tzinfo=ist)
    candles = []
    price = 100.0
    for index in range(40):
        close = price + index * 0.10
        candles.append({
            "date": start + timedelta(minutes=3 * index),
            "open": close - 0.05,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": 1000 + index * 10,
        })
    now = start + timedelta(minutes=3 * 40)
    metrics = completed_context_metrics(
        candles,
        {
            "change_pct": 2.0,
            "last_price": candles[-1]["high"],
            "turnover": 200_000_000,
            "spread_pct": 0.05,
        },
        now=now,
        near_breakout_pct=0.50,
        min_rvol=1.0,
        min_atr_pct=0.0,
        max_atr_pct=5.0,
        max_vwap_distance_pct=10.0,
        buy_min_adx=0.0,
        sell_min_adx=0.0,
    )
    assert metrics["context_available"] is True
    assert metrics["near_n20_breakout"] is True
    assert metrics["n20_high"] == round(candles[-1]["high"], 4)
    assert metrics["direction"] == "BUY"
