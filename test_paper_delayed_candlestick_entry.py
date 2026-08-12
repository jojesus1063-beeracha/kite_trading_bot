import pandas as pd

from paper_delayed_candlestick_entry import (
    Direction,
    Pattern,
    add_indicators,
    build_plan,
    confirm_setup,
    detect_setup,
    is_bullish_engulfing,
    is_bullish_marubozu,
    is_doji,
    is_hammer,
    is_inside_bar,
    position_size,
)


def candle(o, h, l, c, v=1000):
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": v})


def base_df():
    rows = []
    for i in range(22):
        rows.append({
            "date": f"2026-08-12 09:{15 + i:02d}:00",
            "open": 100.0,
            "high": 100.3,
            "low": 99.8,
            "close": 100.1,
            "volume": 1000,
        })
    return pd.DataFrame(rows)


def run():
    passed = 0

    assert is_bullish_marubozu(candle(100, 101.02, 99.98, 101.0))
    print("PASS: bullish Marubozu detected")
    passed += 1

    assert is_hammer(candle(100.4, 100.5, 99.7, 100.3))
    print("PASS: Hammer detected")
    passed += 1

    assert is_bullish_engulfing(candle(100.5, 100.6, 99.8, 100.0), candle(99.9, 100.8, 99.8, 100.7))
    print("PASS: bullish engulfing detected")
    passed += 1

    assert is_inside_bar(candle(100, 101, 99, 100.5), candle(100.2, 100.8, 99.2, 100.4))
    print("PASS: inside bar detected")
    passed += 1

    assert is_doji(candle(100, 101, 99, 100.05))
    print("PASS: Doji detected")
    passed += 1

    # 0.20% of Rs5,000 = Rs10 max planned risk.
    qty = position_size(5000, 100.0, 99.5, 0.20)
    assert qty == 20
    print("PASS: PAPER sizing uses 0.20% risk (Rs10 planned risk)")
    passed += 1

    plan = build_plan(Pattern.HAMMER, Direction.BUY, 20, 21, 100.0, 99.5, 5000, 0.20)
    assert plan is not None
    assert round(plan.target_price, 2) == 101.0
    assert plan.rr == 2.0
    assert round(plan.total_risk, 2) == 10.0
    print("PASS: 2R TP and risk plan are correct")
    passed += 1

    # Build a closed Hammer on index 20 with volume > SMA20 and close above EMA/VWAP.
    df = base_df()
    df.loc[20, ["open", "high", "low", "close", "volume"]] = [100.4, 100.5, 99.7, 100.3, 2000]
    enriched = add_indicators(df)
    setup = detect_setup(enriched, 20, 0.05)
    assert setup is not None and setup.pattern == Pattern.HAMMER
    print("PASS: closed Hammer creates pending setup")
    passed += 1

    # Next closed candle must CLOSE above trigger 100.55; intrabar high alone is insufficient.
    df.loc[21, ["open", "high", "low", "close", "volume"]] = [100.3, 100.7, 100.2, 100.50, 1800]
    enriched = add_indicators(df)
    setup = detect_setup(enriched, 20, 0.05)
    assert confirm_setup(enriched, setup, 21, 5000, 0.05, 0.20) is None
    print("PASS: intrabar touch does not trigger; closed-candle confirmation enforced")
    passed += 1

    df.loc[21, ["open", "high", "low", "close", "volume"]] = [100.3, 100.8, 100.2, 100.65, 1800]
    enriched = add_indicators(df)
    setup = detect_setup(enriched, 20, 0.05)
    confirmed = confirm_setup(enriched, setup, 21, 5000, 0.05, 0.20)
    assert confirmed is not None and confirmed.direction == Direction.BUY
    print("PASS: closed candle above Hammer trigger confirms entry")
    passed += 1

    print(f"\nResults: {passed} passed, 0 failed")


if __name__ == "__main__":
    run()
