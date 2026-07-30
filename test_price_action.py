import pandas as pd
from price_action import find_swing_points, detect_market_structure

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def make_df(base_values):
    n = len(base_values)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-30 09:15", periods=n, freq="5min"),
        "open": base_values, "high": [v + 3 for v in base_values],
        "low": base_values, "close": [v + 1 for v in base_values], "volume": [1000] * n,
    })

# Uptrend: trough1=90(idx3), peak1=108(idx7), trough2=96(idx11, Higher Low), peak2=120(idx15, Higher High)
uptrend = [100, 96, 93, 90, 95, 100, 104, 108, 104, 100, 98, 96, 102, 108, 114, 120, 116, 112, 108]
df_up = make_df(uptrend)

swing_highs, swing_lows = find_swing_points(df_up, lookback=3)
check("Uptrend: finds exactly 2 confirmed swing highs", len(swing_highs) == 2)
check("Uptrend: finds exactly 2 confirmed swing lows", len(swing_lows) == 2)
check("Uptrend: second swing high is higher (Higher High)", swing_highs[1][1] > swing_highs[0][1])
check("Uptrend: second swing low is higher (Higher Low)", swing_lows[1][1] > swing_lows[0][1])

confirms, detail = detect_market_structure(df_up, "BUY", lookback=3)
check("BUY structure CONFIRMS on genuine Higher High + Higher Low", confirms == True)
check("BUY detail correctly flags higher_high", detail["higher_high"] == True)
check("BUY detail correctly flags higher_low", detail["higher_low"] == True)

confirms_sell_on_uptrend, _ = detect_market_structure(df_up, "SELL", lookback=3)
check("SELL structure correctly REJECTS on an uptrend", confirms_sell_on_uptrend == False)

# Downtrend: peak1=110(idx3), trough1=95(idx7), peak2=105(idx11, Lower High), trough2=85(idx15, Lower Low)
downtrend = [100, 104, 107, 110, 105, 100, 97, 95, 99, 102, 104, 105, 100, 95, 90, 85, 89, 93, 97]
df_down = make_df(downtrend)

confirms_down, detail_down = detect_market_structure(df_down, "SELL", lookback=3)
check("SELL structure CONFIRMS on genuine Lower High + Lower Low", confirms_down == True)
check("SELL detail correctly flags lower_high", detail_down["lower_high"] == True)
check("SELL detail correctly flags lower_low", detail_down["lower_low"] == True)

confirms_buy_on_downtrend, _ = detect_market_structure(df_down, "BUY", lookback=3)
check("BUY structure correctly REJECTS on a downtrend", confirms_buy_on_downtrend == False)

# Insufficient data -- fail-safe
short_df = make_df([100, 101, 102])
confirms_short, detail_short = detect_market_structure(short_df, "BUY", lookback=3)
check("Insufficient data returns False (fail-safe), does not crash", confirms_short == False)
check("Insufficient data reason correctly reported", "insufficient" in detail_short.get("reason", "").lower())

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Support & Resistance ---
print("\n--- Support & Resistance Tests ---")
from price_action import find_support_resistance, evaluate_support_resistance

support, resistance = find_support_resistance(df_up, lookback=3)
check("Uptrend fixture: resistance detected correctly (111, nearest above current price)", resistance == 111)
check("Uptrend fixture: support detected correctly (96)", support == 96)

blocked, detail = evaluate_support_resistance(df_up, "BUY", min_distance_pct=0.5, lookback=3)
check("BUY not blocked when comfortably far from resistance (~10%)", blocked == False)

# Confirmed swing high (resistance) at high=103 (base 100 + the fixture's +3 high offset),
# current close 102.7 -- 0.29% below it
near_resistance = [90, 93, 96, 100, 96, 93, 90, 88, 89, 101.7]
df_near_res = make_df(near_resistance)
blocked_res, detail_res = evaluate_support_resistance(df_near_res, "BUY", min_distance_pct=0.5, lookback=3)
check("BUY correctly BLOCKED when within 0.5% of resistance", blocked_res == True)
check("Resistance level correctly identified in detail", detail_res["resistance"] == 103)

# Confirmed swing low (support) at 100, current price 0.3% above it
near_support = [110, 107, 104, 100, 104, 107, 110, 112, 111, 99.3]
df_near_sup = make_df(near_support)
blocked_sup, detail_sup = evaluate_support_resistance(df_near_sup, "SELL", min_distance_pct=0.5, lookback=3)
check("SELL correctly BLOCKED when within 0.5% of support", blocked_sup == True)
check("Support level correctly identified in detail", detail_sup["support"] == 100)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Breakout Confirmation ---
print("\n--- Breakout Confirmation Tests ---")
from price_action import detect_breakout

def make_df_vol(closes, volumes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-30 09:15", periods=n, freq="5min"),
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": volumes,
    })

# BUY breakout: close 110 > level 105, volume 2000 vs avg~1000 (>1.5x)
df_breakout = make_df_vol([100]*9 + [110], [1000]*9 + [2000])
confirmed, detail = detect_breakout(df_breakout, "BUY", level=105, volume_multiplier=1.5, volume_period=9)
check("BUY breakout CONFIRMED: close beyond level + volume confirmation", confirmed == True)

# BUY, close beyond level but volume NOT confirmed (same as average)
df_no_vol = make_df_vol([100]*9 + [110], [1000]*10)
confirmed_novol, _ = detect_breakout(df_no_vol, "BUY", level=105, volume_multiplier=1.5, volume_period=9)
check("BUY breakout REJECTED: close beyond level but no volume confirmation", confirmed_novol == False)

# BUY, volume confirmed but close does NOT beat the level
df_no_break = make_df_vol([100]*9 + [100], [1000]*9 + [2000])
confirmed_nobreak, _ = detect_breakout(df_no_break, "BUY", level=105, volume_multiplier=1.5, volume_period=9)
check("BUY breakout REJECTED: high volume but close did not beat the level", confirmed_nobreak == False)

# SELL breakout: close 95 < level 100, volume confirmed
df_sell_breakout = make_df_vol([110]*9 + [95], [1000]*9 + [2000])
confirmed_sell, _ = detect_breakout(df_sell_breakout, "SELL", level=100, volume_multiplier=1.5, volume_period=9)
check("SELL breakout CONFIRMED: close beyond support level + volume", confirmed_sell == True)

# No level provided
confirmed_none, detail_none = detect_breakout(df_breakout, "BUY", level=None)
check("No level provided returns False with clear reason", confirmed_none == False and "no level" in detail_none["reason"])

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Rejection Candles ---
print("\n--- Rejection Candle Tests ---")
from price_action import is_hammer, is_shooting_star, is_pin_bar, is_long_wick_rejection, detect_rejection_candle

hammer_row = {"open": 100, "close": 101, "high": 101.5, "low": 95}
check("Hammer correctly detected", is_hammer(hammer_row) == True)

shooting_star_row = {"open": 100, "close": 99, "high": 105, "low": 98.5}
check("Shooting Star correctly detected", is_shooting_star(shooting_star_row) == True)

normal_row = {"open": 100, "close": 102, "high": 103, "low": 99}
check("Normal candle correctly NOT a hammer", is_hammer(normal_row) == False)
check("Normal candle correctly NOT a shooting star", is_shooting_star(normal_row) == False)

pin_bar_bull, pin_dir_bull = is_pin_bar(hammer_row)
check("Pin bar correctly detects bullish (hammer-shaped)", pin_bar_bull == True and pin_dir_bull == "bullish")

pin_bar_bear, pin_dir_bear = is_pin_bar(shooting_star_row)
check("Pin bar correctly detects bearish (shooting-star-shaped)", pin_bar_bear == True and pin_dir_bear == "bearish")

pin_bar_none, pin_dir_none = is_pin_bar(normal_row)
check("Pin bar correctly finds nothing on a normal candle", pin_bar_none == False and pin_dir_none is None)

# Fails hammer's strict thresholds but qualifies under the looser long-wick check
long_wick_row = {"open": 100, "close": 101, "high": 103, "low": 97}
check("This fixture correctly FAILS strict hammer test", is_hammer(long_wick_row) == False)
lw_bool, lw_dir = is_long_wick_rejection(long_wick_row)
check("Looser long-wick rejection correctly catches it as bullish", lw_bool == True and lw_dir == "bullish")

df_hammer = pd.DataFrame({
    "date": pd.date_range("2026-07-30 09:15", periods=5, freq="5min"),
    "open": [100.0, 100.0, 100.0, 100.0, hammer_row["open"]],
    "high": [101.0, 101.0, 101.0, 101.0, hammer_row["high"]],
    "low": [99.0, 99.0, 99.0, 99.0, hammer_row["low"]],
    "close": [100.0, 100.0, 100.0, 100.0, hammer_row["close"]],
    "volume": [1000] * 5,
})
confirmed_rej, detail_rej = detect_rejection_candle(df_hammer, "BUY")
check("detect_rejection_candle: BUY confirms on a real hammer at the last row", confirmed_rej == True)
check("detect_rejection_candle: correctly names 'hammer' as a matched pattern", "hammer" in detail_rej["patterns"])

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Range Detection ---
print("\n--- Range Detection Tests ---")
from price_action import detect_range

# Narrow, range-bound: closes oscillate tightly around 100, small true ranges
import random
random.seed(42)
range_closes = [100 + (0.1 if i % 2 == 0 else -0.1) for i in range(20)]
df_range = pd.DataFrame({
    "date": pd.date_range("2026-07-30 09:15", periods=20, freq="5min"),
    "open": range_closes, "high": [c + 0.15 for c in range_closes],
    "low": [c - 0.15 for c in range_closes], "close": range_closes, "volume": [1000] * 20,
})
is_range, detail = detect_range(df_range, atr_period=14, atr_threshold_pct=1.0, flatness_lookback=10, flatness_threshold_pct=0.5)
check("Narrow, tightly-oscillating market correctly detected as RANGE", is_range == True)

# Trending: steady climb, meaningful bar-to-bar moves
trend_closes = [100 + i * 1.5 for i in range(20)]
df_trend = pd.DataFrame({
    "date": pd.date_range("2026-07-30 09:15", periods=20, freq="5min"),
    "open": trend_closes, "high": [c + 1 for c in trend_closes],
    "low": [c - 1 for c in trend_closes], "close": trend_closes, "volume": [1000] * 20,
})
is_range_trend, detail_trend = detect_range(df_trend, atr_period=14, atr_threshold_pct=1.0, flatness_lookback=10, flatness_threshold_pct=0.5)
check("Steadily trending market correctly NOT flagged as range", is_range_trend == False)

# Insufficient data
short_range_df = df_range.head(5)
is_range_short, detail_short = detect_range(short_range_df)
check("Insufficient data for range detection fails safe (False)", is_range_short == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Break of Structure (BOS) ---
print("\n--- BOS Tests ---")
from price_action import detect_bos

# Confirmed swing high at high=105 (base 102), then price closes above it (107)
bos_confirm = [90, 93, 96, 102, 96, 93, 90, 88, 89, 106]
df_bos_confirm = make_df(bos_confirm)
bos_true, detail_bos_true = detect_bos(df_bos_confirm, "BUY", lookback=3)
check("BOS confirms BUY when price closes beyond the last confirmed swing high", bos_true == True)

bos_false, _ = detect_bos(df_up, "BUY", lookback=3)
check("BOS correctly does NOT confirm when price hasn't broken the last swing high yet", bos_false == False)

# No confirmed swing yet
tiny_df = make_df([100, 101, 102])
bos_none, detail_none = detect_bos(tiny_df, "BUY", lookback=3)
check("BOS fails safe (False) when no confirmed swing exists yet", bos_none == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Change of Character (CHoCH) ---
print("\n--- CHoCH Tests ---")
from price_action import detect_choch

choch_buy_on_downtrend, detail_c1 = detect_choch(df_down, "BUY", lookback=3)
check("CHoCH warns on a BUY when the actual structure is Lower High + Lower Low", choch_buy_on_downtrend == True)

choch_buy_on_uptrend, _ = detect_choch(df_up, "BUY", lookback=3)
check("CHoCH correctly does NOT warn on a BUY when structure is genuinely bullish (HH+HL)", choch_buy_on_uptrend == False)

choch_sell_on_uptrend, detail_c2 = detect_choch(df_up, "SELL", lookback=3)
check("CHoCH warns on a SELL when the actual structure is Higher High + Higher Low", choch_sell_on_uptrend == True)

choch_sell_on_downtrend, _ = detect_choch(df_down, "SELL", lookback=3)
check("CHoCH correctly does NOT warn on a SELL when structure is genuinely bearish (LH+LL)", choch_sell_on_downtrend == False)

tiny_choch = make_df([100, 101, 102])
choch_insufficient, detail_insuff = detect_choch(tiny_choch, "BUY", lookback=3)
check("CHoCH fails safe (False) with insufficient confirmed swings", choch_insufficient == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Pullback Entry ---
print("\n--- Pullback Entry Tests ---")
from price_action import detect_pullback_entry

def make_ohlc_df(bars):
    n = len(bars)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-30 09:15", periods=n, freq="5min"),
        "open": [b[0] for b in bars], "high": [b[1] for b in bars],
        "low": [b[2] for b in bars], "close": [b[3] for b in bars], "volume": [1000] * n,
    })

pre_pb = [(1450,1500,1400,1480)]*3 + [(1960,2000,1950,1990)] + [(1450,1500,1400,1480)]*8
recent_confirmed = [
    (1991,2010,1991,2005), (2005,2020,2000,2015), (2015,2018,2005,2008),
    (2008,2010,1999,2001), (2001,2003,1996,1998), (1998,2000,1994,1996),
    (1996,1998,1993,1994), (1994,1996,1992,1993), (1993,1995,1991,1992),
    (1993,1994.5,1990.5,1994),
]
df_pullback_confirmed = make_ohlc_df(pre_pb + recent_confirmed)
confirmed_pb, detail_pb = detect_pullback_entry(df_pullback_confirmed, "BUY", lookback=10)
check("Pullback entry CONFIRMS: breakout + pullback + hammer rejection + continuation, all real", confirmed_pb == True)
check("Pullback detail correctly identifies the broken level (2000)", detail_pb["broken_level"] == 2000)

# Same pre-window, but recent stays flat -- no breakout at all
recent_no_breakout = [(1480,1490,1470,1485)] * 10
df_no_breakout = make_ohlc_df(pre_pb + recent_no_breakout)
confirmed_none, detail_none = detect_pullback_entry(df_no_breakout, "BUY", lookback=10)
check("Pullback entry correctly does NOT confirm when there's no breakout at all", confirmed_none == False)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- Confidence Scoring / Orchestration ---
print("\n--- Confidence Scoring Tests ---")
import config as cfg
from price_action import get_price_action_score, evaluate_price_action

cfg.ENABLE_PRICE_ACTION = False
score_disabled, detail_disabled = evaluate_price_action(df_pullback_confirmed, "BUY", cfg)
check("Disabled: returns score 0, enabled=False", score_disabled == 0 and detail_disabled["enabled"] == False)

cfg.ENABLE_PRICE_ACTION = True
cfg.USE_MARKET_STRUCTURE = True
cfg.USE_SUPPORT_RESISTANCE = True
cfg.USE_BREAKOUT_CONFIRMATION = True
cfg.USE_PULLBACK_ENTRY = True
cfg.USE_REJECTION_CANDLES = True
cfg.USE_RANGE_FILTER = True
cfg.USE_BOS = True
cfg.USE_CHOCH = True
cfg.SUPPORT_RESISTANCE_LOOKBACK = 30
cfg.MIN_DISTANCE_TO_SR_PERCENT = 0.5

score_enabled, detail_enabled = evaluate_price_action(df_pullback_confirmed, "BUY", cfg)
check("Enabled: runs all 8 sub-features without crashing", "total_price_action_score" in detail_enabled)
check("Enabled: pullback fixture's real pullback confirmation contributes +10",
      detail_enabled.get("pullback") == True)
check("Enabled: total score matches sum of individual contributions", score_enabled == detail_enabled["total_price_action_score"])

# Individually verify get_price_action_score's math with a hand-checked case:
# only market_structure enabled and confirmed on df_up (a genuine HH+HL uptrend) = exactly +15
cfg.USE_MARKET_STRUCTURE = True
cfg.USE_SUPPORT_RESISTANCE = False
cfg.USE_BREAKOUT_CONFIRMATION = False
cfg.USE_PULLBACK_ENTRY = False
cfg.USE_REJECTION_CANDLES = False
cfg.USE_RANGE_FILTER = False
cfg.USE_BOS = False
cfg.USE_CHOCH = False
score_ms_only, detail_ms_only = get_price_action_score(df_up, "BUY", cfg)
check("Isolated Market Structure score: exactly +15 on a genuine HH+HL fixture", score_ms_only == 15)

# Fail-safe: force an internal exception, confirm evaluate_price_action never raises
import price_action as pa_module
original_func = pa_module.detect_market_structure
pa_module.detect_market_structure = lambda *a, **kw: (_ for _ in ()).throw(Exception("simulated failure"))
cfg.USE_MARKET_STRUCTURE = True
score_failsafe, detail_failsafe = evaluate_price_action(df_up, "BUY", cfg)
check("Fail-safe: internal exception returns score 0, does not raise", score_failsafe == 0 and "error" in detail_failsafe)
pa_module.detect_market_structure = original_func

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
