import copy
import sys
import types

fake_auth = types.ModuleType("auth")
fake_auth.get_kite_client = lambda: None
sys.modules.setdefault("auth", fake_auth)
sys.modules.setdefault("requests", types.ModuleType("requests"))

from paper_nse_top_movers_selector import select_top_movers


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
