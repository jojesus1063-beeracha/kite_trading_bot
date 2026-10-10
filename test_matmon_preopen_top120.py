import math

import matmon_preopen_top120 as preopen
import matmon_strategy_config as strategy_def
import matmon_watchlist_scoring as scoring


def test_top_n_sourced_from_central_strategy_config_not_hardcoded():
    assert preopen.TOP_N == strategy_def.MATMON_WATCHLIST_SIZE
    assert preopen.TOP_N == 120


def _quote(bid, ask, volume, prev_close, last, bid_qty=3000, ask_qty=2000):
    return {
        "last_price": last,
        "volume": volume,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty}],
            "sell": [{"price": ask, "quantity": ask_qty}],
        },
        "ohlc": {"close": prev_close},
    }


def test_evaluate_delegates_to_shared_hard_eligibility():
    row = {"symbol": "ABC", "exchange": "NSE"}
    q = _quote(100.0, 100.10, 50000, 98.0, 101.0)

    via_module = preopen.evaluate(row, q)
    via_scoring, reason = scoring.hard_eligibility(
        row,
        q,
        min_price=preopen.MIN_PRICE,
        max_price=preopen.MAX_PRICE,
        max_spread_pct=preopen.MAX_SPREAD_PCT,
        min_depth_value=preopen.MIN_DEPTH_VALUE,
    )
    assert reason is None
    assert via_module["symbol"] == via_scoring["symbol"] == "ABC"
    assert via_module["depth_value"] == via_scoring["depth_value"]
    assert via_module["gap_pct"] == via_scoring["gap_pct"]


def test_evaluate_still_rejects_ineligible_quotes():
    row = {"symbol": "THIN", "exchange": "NSE"}
    q = _quote(100.0, 100.05, 5000, 100.0, 100.0, bid_qty=1, ask_qty=1)
    assert preopen.evaluate(row, q) is None


def test_model_a_score_matches_original_45_25_20_10_formula():
    # Rebuild the exact pre-refactor formula independently and confirm
    # score_model_a() reproduces it -- this is the regression guard proving
    # the delegation didn't change production selection behavior.
    rows_quotes = [
        ({"symbol": f"S{i}", "exchange": "NSE"},
         _quote(100.0 + i, 100.05 + i, 10000 * (i + 1), 100.0, 100.0 + i,
                bid_qty=2000 + i * 100, ask_qty=1800))
        for i in range(6)
    ]
    candidates = [preopen.evaluate(r, q) for r, q in rows_quotes]
    assert all(c is not None for c in candidates)

    # Independent re-implementation of the ORIGINAL inline formula.
    liq_raw = [math.log1p(c["depth_value"]) for c in candidates]
    vol_raw = [math.log1p(max(0.0, c["volume"])) for c in candidates]
    liq_lo, liq_hi = min(liq_raw), max(liq_raw)
    vol_lo, vol_hi = min(vol_raw), max(vol_raw)
    expected = []
    for c, lr, vr in zip(candidates, liq_raw, vol_raw):
        liq_norm = (lr - liq_lo) / (liq_hi - liq_lo) if liq_hi > liq_lo else 0.0
        vol_norm = (vr - vol_lo) / (vol_hi - vol_lo) if vol_hi > vol_lo else 0.0
        spread_quality = max(0.0, 1.0 - (c["spread_pct"] / preopen.MAX_SPREAD_PCT))
        expected.append(round(
            45.0 * liq_norm + 25.0 * vol_norm + 20.0 * spread_quality + 10.0 * c["imbalance"],
            6,
        ))

    scoring.score_model_a(candidates)
    actual = [c["model_a_score"] for c in candidates]
    assert actual == expected


def test_model_b_computed_but_does_not_alter_model_a_score():
    rows_quotes = [
        ({"symbol": f"S{i}", "exchange": "NSE"},
         _quote(100.0 + i, 100.05 + i, 10000 * (i + 1), 100.0, 100.0 + i))
        for i in range(5)
    ]
    candidates = [preopen.evaluate(r, q) for r, q in rows_quotes]
    scoring.score_model_a(candidates)
    a_scores_before = [c["model_a_score"] for c in candidates]
    scoring.score_model_b(candidates)
    a_scores_after = [c["model_a_score"] for c in candidates]
    assert a_scores_before == a_scores_after
    assert all("model_b_score" in c for c in candidates)
