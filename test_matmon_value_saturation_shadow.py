from types import SimpleNamespace

import pandas as pd

from matmon_value_saturation_shadow import add_preopen_value_shadow, evaluate_shadow


def _frame(last_open=109.8, last_high=110.2, last_low=109.6, last_close=110.0):
    rows = []
    start = pd.Timestamp("2026-09-04 09:15:00")
    for i in range(30):
        close = 100.0 + i * 0.34
        rows.append({"date": start + pd.Timedelta(minutes=3 * i), "open": close - 0.15, "high": close + 0.25, "low": close - 0.25, "close": close, "volume": 10000 + i * 100})
    rows[-1].update(open=last_open, high=last_high, low=last_low, close=last_close)
    return pd.DataFrame(rows)


def test_shadow_is_observational_and_json_safe():
    df = _frame()
    pdi = pd.Series([25.0] * 29 + [26.0])
    mdi = pd.Series([15.0] * 30)
    result = evaluate_shadow(df, "BUY", pdi, mdi)
    assert result["available"] is True
    assert result["observation_only"] is True
    assert isinstance(result["would_flag_saturation"], bool)


def test_extension_plus_weakening_flags_possible_saturation():
    df = _frame(last_open=108.0, last_high=115.0, last_low=107.8, last_close=110.0)
    pdi = pd.Series([30.0] * 29 + [21.0])
    mdi = pd.Series([10.0] * 30)
    result = evaluate_shadow(df, "BUY", pdi, mdi)
    assert result["would_flag_saturation"] is True
    assert result["extension_reasons"]
    assert result["weakening_reasons"]


def test_value_shadow_does_not_change_production_scores_or_order():
    rows = [
        {"symbol": "A", "last_price": 100.0, "volume": 1000, "preopen_score": 90.0},
        {"symbol": "B", "last_price": 1000.0, "volume": 1000, "preopen_score": 10.0},
    ]
    before = [(x["symbol"], x["preopen_score"]) for x in rows]
    add_preopen_value_shadow(rows)
    assert before == [(x["symbol"], x["preopen_score"]) for x in rows]
    assert rows[1]["rupee_turnover_percentile_shadow"] > rows[0]["rupee_turnover_percentile_shadow"]
