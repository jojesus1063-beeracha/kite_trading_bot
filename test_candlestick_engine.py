import pandas as pd

from candlestick_engine import (
    CandlestickEngine,
    EngineConfig,
    GateState,
    Pattern,
    Side,
    Setup,
    Trigger,
    add_indicators,
    build_trade_plan,
    context_ok,
    evaluate_trade_entry,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_doji,
    is_hammer,
    is_tweezer_bottom,
    is_tweezer_top,
    position_size,
)


def c(o, h, l, cl, v=2000):
    return pd.Series({"open": o, "high": h, "low": l, "close": cl, "volume": v})


def make_df(n=60):
    rows = []
    price = 100.0
    for i in range(n):
        rows.append({
            "date": pd.Timestamp("2026-08-12 09:15") + pd.Timedelta(minutes=3 * i),
            "open": price,
            "high": price + 0.30,
            "low": price - 0.20,
            "close": price + 0.10,
            "volume": 1000,
        })
        price += 0.05
    return pd.DataFrame(rows)


def run():
    passed = 0

    # Regression: valid red Hammer / demand rejection must pass.
    assert is_hammer(c(100.4, 100.5, 99.7, 100.3))
    print("PASS: red Hammer with 6x lower wick")
    passed += 1

    # Green Hammer must remain valid too.
    assert is_hammer(c(100.3, 100.5, 99.7, 100.4))
    print("PASS: green Hammer remains valid")
    passed += 1

    assert is_bullish_engulfing(c(100.5, 100.6, 99.9, 100.0), c(99.9, 100.8, 99.8, 100.7))
    print("PASS: bullish engulfing")
    passed += 1

    assert is_bearish_engulfing(c(100.0, 100.6, 99.9, 100.5), c(100.6, 100.7, 99.7, 99.8))
    print("PASS: bearish engulfing")
    passed += 1

    assert is_doji(c(100.0, 101.0, 99.0, 100.05))
    print("PASS: Doji")
    passed += 1

    assert is_tweezer_bottom(c(100.5, 100.6, 99.9, 100.0), c(100.0, 100.5, 99.92, 100.4))
    print("PASS: tweezer bottom tolerance")
    passed += 1

    assert is_tweezer_top(c(100.0, 100.5, 99.8, 100.4), c(100.4, 100.52, 99.9, 100.0))
    print("PASS: tweezer top tolerance")
    passed += 1

    # PAPER risk is 0.20%: Rs5,000 -> Rs10 max planned risk.
    assert position_size(5000, 100.0, 99.5, 0.20) == 20
    print("PASS: 0.20% PAPER sizing")
    passed += 1

    cfg = EngineConfig(risk_pct=0.20, min_rr=2.0)
    s = Setup(Pattern.HAMMER, Side.BUY, Trigger.BREAKOUT, 20, (20,), 100.55, 99.50, "test")
    p = build_trade_plan(s, 21, 100.60, 5000, cfg)
    assert p is not None and round(p.target_price, 2) == 102.80 and p.rr == 2.0
    assert p.planned_risk <= 10.0
    print("PASS: 2R target and <=0.20% planned risk")
    passed += 1

    df = make_df()
    enriched = add_indicators(df, cfg)
    last = enriched.iloc[-1]
    assert context_ok(last, Side.BUY)
    assert not context_ok(last, Side.SELL)
    print("PASS: strict VWAP AND EMA50 long context")
    passed += 1

    engine = CandlestickEngine(cfg)

    # Invalid/BLOCK upstream direction cannot be converted into a trade here.
    result = evaluate_trade_entry("TEST", df, "BLOCK", 5000, 0.05, engine)
    assert result.state == GateState.NO_PATTERN
    print("PASS: candlestick gate never overrides upstream BLOCK")
    passed += 1

    # Router returns explicit state and keeps side restricted to intended direction.
    result = evaluate_trade_entry("TEST", df, "BUY", 5000, 0.05, engine)
    assert result.state in {GateState.NO_PATTERN, GateState.WAITING, GateState.CONFIRMED}
    if result.plan is not None:
        assert result.plan.side == Side.BUY
        assert result.plan.planned_risk <= 10.0
        assert result.plan.rr >= 2.0
    if result.setup is not None:
        assert result.setup.side == Side.BUY
    print("PASS: BUY router cannot emit SELL setup/plan")
    passed += 1

    # Mirror directional isolation for SELL.
    sell_engine = CandlestickEngine(cfg)
    result = evaluate_trade_entry("TEST2", df, "SELL", 5000, 0.05, sell_engine)
    if result.plan is not None:
        assert result.plan.side == Side.SELL
        assert result.plan.planned_risk <= 10.0
    if result.setup is not None:
        assert result.setup.side == Side.SELL
    print("PASS: SELL router cannot emit BUY setup/plan")
    passed += 1

    print(f"Results: {passed} passed, 0 failed")


if __name__ == "__main__":
    run()
