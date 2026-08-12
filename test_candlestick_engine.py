import pandas as pd

from candlestick_engine import (
    CandlestickEngine, EngineConfig, Pattern, Side, Setup, Trigger,
    add_indicators, build_trade_plan, context_ok, is_bullish_engulfing,
    is_bearish_engulfing, is_doji, is_tweezer_bottom, is_tweezer_top,
    position_size,
)


def c(o,h,l,cl,v=2000):
    return pd.Series({"open":o,"high":h,"low":l,"close":cl,"volume":v})


def make_df(n=60):
    rows=[]
    price=100.0
    for i in range(n):
        rows.append({
            "date": pd.Timestamp("2026-08-12 09:15") + pd.Timedelta(minutes=3*i),
            "open": price,
            "high": price+0.30,
            "low": price-0.20,
            "close": price+0.10,
            "volume": 1000,
        })
        price += 0.05
    return pd.DataFrame(rows)


def run():
    passed=0

    assert is_bullish_engulfing(c(100.5,100.6,99.9,100.0), c(99.9,100.8,99.8,100.7))
    print("PASS: bullish engulfing") ; passed+=1
    assert is_bearish_engulfing(c(100.0,100.6,99.9,100.5), c(100.6,100.7,99.7,99.8))
    print("PASS: bearish engulfing") ; passed+=1
    assert is_doji(c(100.0,101.0,99.0,100.05))
    print("PASS: Doji") ; passed+=1
    assert is_tweezer_bottom(c(100.5,100.6,99.9,100.0), c(100.0,100.5,99.92,100.4))
    print("PASS: tweezer bottom tolerance") ; passed+=1
    assert is_tweezer_top(c(100.0,100.5,99.8,100.4), c(100.4,100.52,99.9,100.0))
    print("PASS: tweezer top tolerance") ; passed+=1

    assert position_size(100000,100.0,99.0,1.0)==1000
    print("PASS: 1% sizing") ; passed+=1

    cfg=EngineConfig(risk_pct=1.0,min_rr=2.0)
    s=Setup(Pattern.HAMMER,Side.BUY,Trigger.BREAKOUT,20,(20,),100.55,99.50,"test")
    p=build_trade_plan(s,21,100.60,100000,cfg)
    assert p is not None and round(p.target_price,2)==102.80 and p.rr==2.0
    assert p.planned_risk <= 1000.0
    print("PASS: 2R target and <=1% planned risk") ; passed+=1

    df=make_df()
    enriched=add_indicators(df,cfg)
    last=enriched.iloc[-1]
    assert context_ok(last,Side.BUY)
    assert not context_ok(last,Side.SELL)
    print("PASS: strict VWAP AND EMA50 long context") ; passed+=1

    # closed-bar only router: engine consumes the final row as already completed.
    engine=CandlestickEngine(cfg)
    plans=engine.on_closed_bar("TEST",df,100000,0.05)
    assert isinstance(plans,list)
    print("PASS: closed-bar router returns TradePlan list") ; passed+=1

    print(f"Results: {passed} passed, 0 failed")


if __name__=="__main__":
    run()
