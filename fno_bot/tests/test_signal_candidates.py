from fno_bot.strategies.signal_candidates import (
    MarketSnapshot, TickPoint, premium_imbalance, premium_rate_of_change,
    underlying_open_vs_prev_close, bid_ask_imbalance, depth_imbalance,
    evaluate_all_candidates,
)


def _snapshot(**overrides):
    base = dict(
        underlying_price=77218.05, underlying_prev_close=77000.0,
        ce_price=307.30, pe_price=196.80,
        ce_best_bid=307.0, ce_best_ask=307.6, ce_best_bid_qty=50, ce_best_ask_qty=40,
        pe_best_bid=196.5, pe_best_ask=197.0, pe_best_bid_qty=30, pe_best_ask_qty=60,
        ce_history=(), pe_history=(),
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def test_premium_imbalance_matches_observed_example_direction():
    # Spec's observed trade: CE=307.30 > PE=196.80 -> the naive rule buys PE.
    # This candidate reproduces that specific naive rule (documented as
    # unvalidated), so this test locks in that it does so correctly --
    # NOT an endorsement that this is the right live rule.
    sig = premium_imbalance(_snapshot())
    assert sig.direction == "PE"
    assert sig.confidence is not None


def test_premium_imbalance_handles_equal_premiums():
    sig = premium_imbalance(_snapshot(ce_price=200.0, pe_price=200.0))
    assert sig.direction == "CE"  # tie-break: ce > pe is False -> falls to CE branch
    assert sig.confidence == 0.0 or sig.confidence < 1e-9


def test_premium_imbalance_rejects_non_positive_premium():
    sig = premium_imbalance(_snapshot(ce_price=0.0))
    assert sig.direction is None


def test_premium_rate_of_change_needs_history():
    sig = premium_rate_of_change(_snapshot())
    assert sig.direction is None
    assert "insufficient" in sig.reason


def test_premium_rate_of_change_picks_faster_rising_leg():
    ce_hist = (TickPoint(300.0, 0.0), TickPoint(310.0, 1.0))
    pe_hist = (TickPoint(196.0, 0.0), TickPoint(197.0, 1.0))
    sig = premium_rate_of_change(_snapshot(ce_history=ce_hist, pe_history=pe_hist))
    assert sig.direction == "CE"


def test_underlying_open_vs_prev_close_bullish_gap():
    sig = underlying_open_vs_prev_close(_snapshot(underlying_price=77218.05, underlying_prev_close=77000.0))
    assert sig.direction == "CE"


def test_underlying_open_vs_prev_close_bearish_gap():
    sig = underlying_open_vs_prev_close(_snapshot(underlying_price=76800.0, underlying_prev_close=77000.0))
    assert sig.direction == "PE"


def test_underlying_open_vs_prev_close_missing_prev_close():
    sig = underlying_open_vs_prev_close(_snapshot(underlying_prev_close=None))
    assert sig.direction is None


def test_bid_ask_imbalance_direction():
    # CE: bid-heavy (50 vs 40) -> pressure +0.111
    # PE: ask-heavy (30 vs 60) -> pressure -0.333
    # CE has the stronger (more positive) pressure -> CE selected
    sig = bid_ask_imbalance(_snapshot())
    assert sig.direction == "CE"


def test_bid_ask_imbalance_missing_depth():
    sig = bid_ask_imbalance(_snapshot(ce_best_bid_qty=None, ce_best_ask_qty=None))
    assert sig.direction is None


def test_depth_imbalance_more_liquid_leg():
    sig = depth_imbalance(_snapshot(ce_best_bid_qty=50, ce_best_ask_qty=40, pe_best_bid_qty=10, pe_best_ask_qty=10))
    assert sig.direction == "CE"  # 90 total vs 20 total


def test_evaluate_all_candidates_never_raises_and_covers_registry():
    results = evaluate_all_candidates(_snapshot())
    names = {r.candidate for r in results}
    assert names == {"premium_imbalance", "premium_rate_of_change",
                      "underlying_open_vs_prev_close", "bid_ask_imbalance", "depth_imbalance"}


def test_evaluate_all_candidates_survives_a_raising_candidate(monkeypatch):
    import fno_bot.strategies.signal_candidates as sc

    def boom(snapshot):
        raise RuntimeError("simulated candidate failure")

    monkeypatch.setitem(sc.CANDIDATE_REGISTRY, "premium_imbalance", boom)
    results = sc.evaluate_all_candidates(_snapshot())
    failed = next(r for r in results if r.candidate == "premium_imbalance")
    assert failed.direction is None
    assert "raised" in failed.reason
    # the other candidates still ran despite one raising
    assert len(results) == 5
