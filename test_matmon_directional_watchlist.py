import math

from matmon_directional_watchlist import daily_metrics


def _candles(direction: int):
    rows = []
    close = 100.0
    for i in range(90):
        # Smooth persistent trend with small counter-days.
        step = 0.35 if (i % 7) != 3 else -0.08
        close *= 1 + direction * step / 100
        rows.append({
            "close": close,
            "high": close * 1.006,
            "low": close * 0.994,
            "volume": 100000 + i * 1000,
        })
    return rows


def test_directional_persistence_identifies_buy():
    m = daily_metrics(_candles(1))
    assert m is not None
    assert m["direction"] == "BUY"
    assert m["buy_score_raw"] > m["sell_score_raw"]
    assert m["efficiency20"] > 0.5


def test_directional_persistence_identifies_sell():
    m = daily_metrics(_candles(-1))
    assert m is not None
    assert m["direction"] == "SELL"
    assert m["sell_score_raw"] > m["buy_score_raw"]
    assert m["efficiency20"] > 0.5


def test_extreme_volatility_is_not_a_bonus():
    calm = _candles(1)
    wild = _candles(1)
    for c in wild:
        c["high"] = c["close"] * 1.08
        c["low"] = c["close"] * 0.92
    calm_m = daily_metrics(calm)
    wild_m = daily_metrics(wild)
    assert calm_m is not None and wild_m is not None
    assert wild_m["volatility_quality"] < calm_m["volatility_quality"]
