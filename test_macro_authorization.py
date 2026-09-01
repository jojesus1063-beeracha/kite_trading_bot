"""
Tests the new _macro_authorization() decision layer directly, covering
all 6 state/direction combinations from the required decision matrix.

Deliberately does NOT hardcode "SBIN should approve" -- per the
explicit requirement, this must validate the DECISION LOGIC, not
hindsight. A separate, real historical replay (audit_sbin_20260807.py,
updated for the new architecture) determines SBIN's actual empirical
outcome using real ADX/EMA-slope data, which this test file does not
have access to and does not assume.
"""

import pandas as pd
from datetime import datetime, timedelta

from strategy import _macro_authorization, _stock_adx, _stock_ema_slope

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeCfg:
    ADX_THRESHOLD = 22.0


def make_15m(adx_values, ema_fast_values):
    """n bars, with explicit, controllable adx and ema_fast sequences."""
    base = datetime(2026, 8, 10, 9, 15)
    rows = []
    for i, (adx, ema) in enumerate(zip(adx_values, ema_fast_values)):
        rows.append({"date": base + timedelta(minutes=15 * i), "adx": adx, "ema_fast": ema})
    return pd.DataFrame(rows)


curr_5m = pd.Series({"date": datetime(2026, 8, 10, 10, 0)})


# -- The 4 unconditional (non-NEUTRAL) cases -- must match the required matrix exactly --

decision, detail = _macro_authorization("BULLISH", "BUY", pd.DataFrame(), curr_5m, FakeCfg())
check("BULLISH + BUY -> ALLOW (normal path unchanged)", decision == "ALLOW" and detail["decision"] == "ALLOW_NORMAL")

decision, detail = _macro_authorization("BULLISH", "SELL", pd.DataFrame(), curr_5m, FakeCfg())
check("BULLISH + SELL -> HARD REJECT (opposing)", decision == "REJECT" and detail["decision"] == "HARD_REJECT")

decision, detail = _macro_authorization("BEARISH", "SELL", pd.DataFrame(), curr_5m, FakeCfg())
check("BEARISH + SELL -> ALLOW (normal path unchanged)", decision == "ALLOW" and detail["decision"] == "ALLOW_NORMAL")

decision, detail = _macro_authorization("BEARISH", "BUY", pd.DataFrame(), curr_5m, FakeCfg())
check("BEARISH + BUY -> HARD REJECT (opposing)", decision == "REJECT" and detail["decision"] == "HARD_REJECT")


# -- NEUTRAL: conditional approval, BOTH outcomes proven, neither assumed --

# ADX above threshold, EMA20 rising -> BUY should be CONDITIONALLY APPROVED
df_good_buy = make_15m(adx_values=[20, 25], ema_fast_values=[100.0, 101.0])  # rising
decision, detail = _macro_authorization("NEUTRAL", "BUY", df_good_buy, curr_5m, FakeCfg())
check("NEUTRAL + BUY, ADX>=threshold AND EMA rising -> CONDITIONAL_APPROVED",
      decision == "ALLOW" and detail["decision"] == "CONDITIONAL_APPROVED")

# ADX below threshold -> BUY should be CONDITIONALLY REJECTED, even with a rising EMA
df_bad_adx = make_15m(adx_values=[15, 18], ema_fast_values=[100.0, 101.0])
decision, detail = _macro_authorization("NEUTRAL", "BUY", df_bad_adx, curr_5m, FakeCfg())
check("NEUTRAL + BUY, ADX BELOW threshold (even with rising EMA) -> CONDITIONAL_REJECTED",
      decision == "REJECT" and detail["decision"] == "CONDITIONAL_REJECTED" and detail["adx_ok"] is False)

# ADX above threshold but EMA falling -> BUY should be CONDITIONALLY REJECTED
df_bad_slope = make_15m(adx_values=[20, 25], ema_fast_values=[101.0, 100.0])  # falling
decision, detail = _macro_authorization("NEUTRAL", "BUY", df_bad_slope, curr_5m, FakeCfg())
check("NEUTRAL + BUY, ADX ok but EMA FALLING -> CONDITIONAL_REJECTED",
      decision == "REJECT" and detail["ema_slope_ok"] is False)

# SELL mirror: ADX ok + EMA falling -> CONDITIONALLY APPROVED
df_good_sell = make_15m(adx_values=[20, 25], ema_fast_values=[101.0, 100.0])
decision, detail = _macro_authorization("NEUTRAL", "SELL", df_good_sell, curr_5m, FakeCfg())
check("NEUTRAL + SELL, ADX>=threshold AND EMA falling -> CONDITIONAL_APPROVED",
      decision == "ALLOW" and detail["decision"] == "CONDITIONAL_APPROVED")

# SELL with EMA rising (wrong direction) -> CONDITIONALLY REJECTED
decision, detail = _macro_authorization("NEUTRAL", "SELL", df_good_buy, curr_5m, FakeCfg())
check("NEUTRAL + SELL, EMA rising (wrong direction for a SELL) -> CONDITIONAL_REJECTED",
      decision == "REJECT" and detail["ema_slope_ok"] is False)

# -- Missing/insufficient data fails safe (REJECT), never crashes, never silently approves --

decision, detail = _macro_authorization("NEUTRAL", "BUY", pd.DataFrame(), curr_5m, FakeCfg())
check("NEUTRAL with no 15m data at all -> fails safe to CONDITIONAL_REJECTED, not a crash",
      decision == "REJECT")

df_one_bar = make_15m(adx_values=[25], ema_fast_values=[100.0])  # only 1 bar -- slope needs 2
decision, detail = _macro_authorization("NEUTRAL", "BUY", df_one_bar, curr_5m, FakeCfg())
check("NEUTRAL with only 1 completed 15m bar (slope undefined) -> fails safe, not a crash",
      decision == "REJECT" and detail["ema_slope_ok"] is False)


# -- Support function sanity checks --

check("_stock_adx returns the latest completed bar's ADX",
      _stock_adx(df_good_buy, curr_5m["date"]) == 25)
check("_stock_ema_slope computes a positive value for a rising EMA",
      _stock_ema_slope(df_good_buy, curr_5m["date"]) == 1.0)
check("_stock_ema_slope computes a negative value for a falling EMA",
      _stock_ema_slope(df_good_sell, curr_5m["date"]) == -1.0)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
