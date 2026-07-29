import pandas as pd
from patterns import is_bear_trap, is_bull_trap

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def make_df(lows, highs, closes):
    n = len(lows)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-29 09:15", periods=n, freq="5min"),
        "open": closes, "high": highs, "low": lows, "close": closes, "volume": [1000] * n,
    })

lows_trap =   [95,96,97,98,99,98,97,96,95,94,93,90]
highs_trap =  [105,104,103,102,101,102,103,104,105,106,107,102]
closes_trap = [100,101,102,103,104,103,102,101,100,99,98,96]
df_bear_trap = make_df(lows_trap, highs_trap, closes_trap)
check("Bear trap detected: broke below support, closed back above", is_bear_trap(df_bear_trap) == True)

# Range-bound: lows oscillate around 95 (support level), never break it,
# final candle also stays comfortably above -- genuinely no trap.
lows_clean =   [96,95,97,96,95,97,96,95,97,96,95,97]
closes_clean = [100,99,101,100,99,101,100,99,101,100,99,101]
highs_clean =  [104,103,105,104,103,105,104,103,105,104,103,105]
df_no_trap = make_df(lows_clean, highs_clean, closes_clean)
check("No bear trap when price stays range-bound, never breaking support", is_bear_trap(df_no_trap) == False)

lows_bulltrap =  [95,96,97,98,99,98,97,96,95,94,93,98]
highs_bulltrap = [105,104,103,102,101,102,103,104,105,106,107,110]
closes_bulltrap=[100,99,98,97,96,97,98,99,100,101,102,104]
df_bull_trap = make_df(lows_bulltrap, highs_bulltrap, closes_bulltrap)
check("Bull trap detected: broke above resistance, closed back below", is_bull_trap(df_bull_trap) == True)

check("Insufficient data returns False (fail-safe)", is_bear_trap(make_df([100,101],[102,103],[101,102])) == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
