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

# --- Tests for get_market_trend() (live-fetch wrapper, mocked) ---
print("\n--- get_market_trend() Tests (mocked) ---")

from unittest.mock import MagicMock
import market_trend as market_trend_module

mock_kite = MagicMock()

# Case 1: fetch fails entirely -- should fail safe to Sideways, not raise
market_trend_module.fetch_candles = None  # will be patched via data_feed import inside function
import data_feed
original_fetch = data_feed.fetch_candles
data_feed.fetch_candles = lambda *a, **kw: (_ for _ in ()).throw(Exception("API down"))
try:
    result = market_trend_module.get_market_trend(mock_kite, cfg)
    check("get_market_trend fails safe to Sideways on fetch error", result == "Sideways")
finally:
    data_feed.fetch_candles = original_fetch

# Case 2: empty data returned -- should fail safe to Sideways
data_feed.fetch_candles = lambda *a, **kw: pd.DataFrame()
try:
    result = market_trend_module.get_market_trend(mock_kite, cfg)
    check("get_market_trend fails safe to Sideways on empty data", result == "Sideways")
finally:
    data_feed.fetch_candles = original_fetch

print(f"\n{'='*50}")
print(f"FINAL Results: {passed} passed, {failed} failed")
print(f"{'='*50}")

# --- Tests for sector mapping and get_sector_trend() ---
print("\n--- Sector Trend Tests ---")

from market_trend import sector_for_symbol, get_sector_trend, SECTOR_MAP

check("HDFCBANK maps to NIFTY BANK", sector_for_symbol("HDFCBANK") == "NIFTY BANK")
check("TCS maps to NIFTY IT", sector_for_symbol("TCS") == "NIFTY IT")
check("Unmapped symbol returns None", sector_for_symbol("SOMERANDOMSTOCK") == None)
check("SECTOR_MAP is non-empty", len(SECTOR_MAP) > 0)

# Unmapped symbol -> Sideways, no fetch attempted at all
data_feed.fetch_candles = lambda *a, **kw: (_ for _ in ()).throw(Exception("should not be called"))
try:
    result = get_sector_trend(mock_kite, "SOMERANDOMSTOCK", cfg)
    check("get_sector_trend fails safe to Sideways for unmapped symbol (no fetch attempted)",
          result == "Sideways")
finally:
    data_feed.fetch_candles = original_fetch

# Mapped symbol, fetch error -> Sideways
data_feed.fetch_candles = lambda *a, **kw: (_ for _ in ()).throw(Exception("API down"))
try:
    result = get_sector_trend(mock_kite, "HDFCBANK", cfg)
    check("get_sector_trend fails safe to Sideways on fetch error for mapped symbol",
          result == "Sideways")
finally:
    data_feed.fetch_candles = original_fetch

print(f"\n{'='*50}")
print(f"FINAL Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
import sys
sys.exit(1 if failed else 0)
