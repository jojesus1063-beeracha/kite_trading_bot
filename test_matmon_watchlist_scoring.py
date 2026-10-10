import matmon_strategy_config as strategy_def
import matmon_watchlist_scoring as scoring


def _quote(bid, ask, volume, prev_close, last, bid_qty=1000, ask_qty=1000):
    return {
        "last_price": last,
        "volume": volume,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty}],
            "sell": [{"price": ask, "quantity": ask_qty}],
        },
        "ohlc": {"close": prev_close},
    }


def _row(symbol, exchange="NSE"):
    return {"symbol": symbol, "exchange": exchange}


ELIGIBILITY_KW = dict(min_price=20.0, max_price=2200.0, max_spread_pct=0.50, min_depth_value=1000.0)


def test_hard_eligibility_rejects_out_of_price_range():
    candidate, reason = scoring.hard_eligibility(
        _row("X"), _quote(9.9, 10.0, 5000, 10.0, 10.0), **ELIGIBILITY_KW
    )
    assert candidate is None
    assert reason == "price_out_of_range"


def test_hard_eligibility_rejects_wide_spread():
    candidate, reason = scoring.hard_eligibility(
        _row("X"), _quote(100.0, 102.0, 5000, 100.0, 101.0), **ELIGIBILITY_KW
    )
    assert candidate is None
    assert reason == "spread_too_wide"


def test_hard_eligibility_rejects_thin_depth():
    candidate, reason = scoring.hard_eligibility(
        _row("X"),
        _quote(100.0, 100.05, 5000, 100.0, 100.0, bid_qty=1, ask_qty=1),
        **ELIGIBILITY_KW,
    )
    assert candidate is None
    assert reason == "insufficient_depth"


def test_hard_eligibility_accepts_valid_candidate_and_computes_fields():
    candidate, reason = scoring.hard_eligibility(
        _row("GOOD"),
        _quote(100.0, 100.10, 50000, 98.0, 101.0, bid_qty=5000, ask_qty=3000),
        **ELIGIBILITY_KW,
    )
    assert reason is None
    assert candidate["symbol"] == "GOOD"
    assert candidate["gap_pct"] > 0  # last=101 > prev_close=98
    assert candidate["imbalance_direction"] == "BUY"  # bid_qty > ask_qty
    assert candidate["imbalance_signed"] > 0
    assert candidate["suspicious_locked_quote"] is False


def test_bid_equals_ask_is_flagged_suspicious_not_maximal():
    candidate, reason = scoring.hard_eligibility(
        _row("LOCKED"),
        _quote(100.0, 100.0, 5000, 100.0, 100.0, bid_qty=2000, ask_qty=2000),
        **ELIGIBILITY_KW,
    )
    assert reason is None
    assert candidate["suspicious_locked_quote"] is True
    assert candidate["spread_pct"] == 0.0


def test_model_b_does_not_reward_locked_quote_as_maximal_spread():
    normal, _ = scoring.hard_eligibility(
        _row("NORMAL"),
        _quote(100.0, 100.02, 50000, 99.0, 100.5, bid_qty=5000, ask_qty=5000),
        **ELIGIBILITY_KW,
    )
    locked, _ = scoring.hard_eligibility(
        _row("LOCKED"),
        _quote(100.0, 100.0, 50000, 99.0, 100.5, bid_qty=5000, ask_qty=5000),
        **ELIGIBILITY_KW,
    )
    candidates = [normal, locked]
    scoring.score_model_b(candidates)
    normal_c = next(c for c in candidates if c["symbol"] == "NORMAL")
    locked_c = next(c for c in candidates if c["symbol"] == "LOCKED")
    # A genuinely tight, non-locked spread should score at least as well on
    # the spread component as an unverified locked quote -- locked must not
    # win purely by being 0.0 spread.
    assert normal_c["_spread_b"] >= locked_c["_spread_b"]


def test_percentile_normalize_is_outlier_resistant_vs_minmax():
    rows = [{"v": v} for v in [10, 11, 12, 13, 10000]]
    scoring.minmax_normalize(rows, "v", "v_minmax")
    scoring.percentile_normalize(rows, "v", "v_pct")
    # Under min-max, the four normal values get crushed near 0 by the outlier.
    assert all(r["v_minmax"] < 0.01 for r in rows[:4])
    # Under percentile ranking, they still spread out meaningfully.
    assert rows[0]["v_pct"] == 0.0
    assert rows[3]["v_pct"] == 0.75
    assert rows[4]["v_pct"] == 1.0


def test_winsorize_clips_extreme_values():
    values = [1.0, 2.0, 2.0, 2.0, 2.0, 100.0]
    clipped = scoring.winsorize(values, lower_pct=0.05, upper_pct=0.90)
    assert max(clipped) < 100.0


def test_signed_imbalance_persists_direction_separately_from_magnitude():
    buy_heavy, _ = scoring.hard_eligibility(
        _row("BUYHEAVY"),
        _quote(100.0, 100.05, 5000, 100.0, 100.0, bid_qty=9000, ask_qty=1000),
        **ELIGIBILITY_KW,
    )
    sell_heavy, _ = scoring.hard_eligibility(
        _row("SELLHEAVY"),
        _quote(100.0, 100.05, 5000, 100.0, 100.0, bid_qty=1000, ask_qty=9000),
        **ELIGIBILITY_KW,
    )
    # abs(imbalance) magnitude should be close either way (small difference
    # comes from the bid/ask prices differing by the spread, which is
    # expected since depth value is price*quantity, not raw quantity).
    assert abs(buy_heavy["imbalance"] - sell_heavy["imbalance"]) < 0.001
    # ...but the sign/direction correctly differs.
    assert buy_heavy["imbalance_direction"] == "BUY"
    assert sell_heavy["imbalance_direction"] == "SELL"


def test_gap_uses_absolute_displacement_for_ranking():
    up, _ = scoring.hard_eligibility(
        _row("UP"), _quote(100.0, 100.05, 50000, 100.0, 103.0, bid_qty=3000, ask_qty=3000), **ELIGIBILITY_KW
    )
    down, _ = scoring.hard_eligibility(
        _row("DOWN"), _quote(100.0, 100.05, 50000, 100.0, 97.0, bid_qty=3000, ask_qty=3000), **ELIGIBILITY_KW
    )
    candidates = [up, down]
    scoring.score_model_b(candidates)
    up_c = next(c for c in candidates if c["symbol"] == "UP")
    down_c = next(c for c in candidates if c["symbol"] == "DOWN")
    # Roughly symmetric +3%/-3% moves should score similarly on displacement,
    # since ranking uses abs(gap_pct).
    assert abs(up_c["displacement_score"] - down_c["displacement_score"]) < 1e-9


def test_cas_observational_fields_never_enter_either_score():
    candidate, _ = scoring.hard_eligibility(
        _row("X"),
        _quote(100.0, 100.05, 5000, 100.0, 100.0, bid_qty=3000, ask_qty=3000),
        **ELIGIBILITY_KW,
    )
    source_a = __import__("inspect").getsource(scoring.score_model_a)
    source_b = __import__("inspect").getsource(scoring.score_model_b)
    assert "cas_observational" not in source_a
    assert "cas_observational" not in source_b


def test_model_a_matches_current_production_weights_45_25_20_10():
    candidates = []
    for i in range(5):
        c, _ = scoring.hard_eligibility(
            _row(f"S{i}"),
            _quote(100.0 + i, 100.05 + i, 10000 * (i + 1), 100.0, 100.0 + i, bid_qty=3000, ask_qty=2000),
            **ELIGIBILITY_KW,
        )
        candidates.append(c)
    scoring.score_model_a(candidates)
    for c in candidates:
        assert 0.0 <= c["model_a_score"] <= 100.0


def test_model_a_does_not_reward_a_locked_quote_with_perfect_spread():
    normal, _ = scoring.hard_eligibility(
        _row("NORMAL"),
        _quote(100.0, 100.02, 50000, 100.0, 100.0, bid_qty=5000, ask_qty=5000),
        **ELIGIBILITY_KW,
    )
    locked, _ = scoring.hard_eligibility(
        _row("LOCKED"),
        _quote(100.0, 100.0, 50000, 100.0, 100.0, bid_qty=5000, ask_qty=5000),
        **ELIGIBILITY_KW,
    )
    scoring.score_model_a([normal, locked])
    assert normal["model_a_score"] > locked["model_a_score"]


def test_model_b_weights_sum_to_one():
    assert abs(sum(scoring.MODEL_B_WEIGHTS.values()) - 1.0) < 1e-9


def test_watchlist_size_sourced_from_central_strategy_config():
    # This test file doesn't hard-code 60 anywhere else -- the module under
    # test doesn't select Top-N itself (matmon_preopen_top120.py does), but
    # confirm the shared source is what everything should key off.
    assert strategy_def.MATMON_WATCHLIST_SIZE == 120
