"""
Unit tests for market_trend.classify_trend() -- pure logic, no API calls.
Run: python3 test_market_trend.py
"""
import pandas as pd
import config as cfg
from market_trend import classify_trend

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"PASS: {name}")
        passed += 1
    else:
        print(f"FAIL: {name}")
        failed += 1

def make_row(close, ema_fast, ema_slow, vwap, adx=30.0):
    return pd.DataFrame([{
        "date": pd.Timestamp("2026-07-29 10:00"),
        "close": close, "ema_fast": ema_fast, "ema_slow": ema_slow,
        "vwap": vwap, "adx": adx,
    }])

# Bullish: close > ema_fast > ema_slow AND close > vwap
df_bullish = make_row(close=25100, ema_fast=25050, ema_slow=25000, vwap=25020)
check("Clear uptrend classifies as Bullish", classify_trend(df_bullish, cfg) == "Bullish")

# Bearish: close < ema_fast < ema_slow AND close < vwap
df_bearish = make_row(close=24900, ema_fast=24950, ema_slow=25000, vwap=24980)
check("Clear downtrend classifies as Bearish", classify_trend(df_bearish, cfg) == "Bearish")

# Sideways: EMAs not aligned in either direction
df_sideways = make_row(close=25000, ema_fast=25010, ema_slow=25005, vwap=25050)
check("Non-aligned EMAs classify as Sideways", classify_trend(df_sideways, cfg) == "Sideways")

# Sideways: price above EMAs but below VWAP (mixed signal, no clean trend)
df_mixed = make_row(close=25060, ema_fast=25050, ema_slow=25000, vwap=25100)
check("Mixed signal (EMA up, price below VWAP) classifies as Sideways",
      classify_trend(df_mixed, cfg) == "Sideways")

# Empty DataFrame -- fail-safe, should not crash
df_empty = pd.DataFrame()
check("Empty DataFrame returns Sideways (fail-safe, no crash)",
      classify_trend(df_empty, cfg) == "Sideways")

# NaN EMA (insufficient warm-up data) -- should be Sideways, not crash
df_nan = make_row(close=25000, ema_fast=25000, ema_slow=float("nan"), vwap=25000)
check("NaN EMA (insufficient warm-up) returns Sideways, no crash",
      classify_trend(df_nan, cfg) == "Sideways")

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
import sys
sys.exit(1 if failed else 0)
