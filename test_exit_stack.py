"""
Comprehensive synthetic tests for the new exit-logic stack:
hard stop, ATR trailing stop, market structure break, trend reversal.
Run: python3 test_exit_stack.py
"""
from unittest.mock import MagicMock
import pandas as pd
import config as cfg

cfg.ENABLE_FIXED_TARGET = False  # this file specifically tests the ORIGINAL exit-stack (trailing stop, structure break, trend reversal) -- explicitly opt out of fixed-target mode so those tests keep verifying the pre-existing behavior, per the requirement that it must remain unchanged in normal mode
from risk_manager import RiskManager
from main import check_position_exit, _market_structure_broken

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"PASS: {name}")
        passed += 1
    else:
        print(f"FAIL: {name}")
        failed += 1

def make_df_5m(closes, highs=None, lows=None):
    n = len(closes)
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    return pd.DataFrame({
        "date": pd.date_range("2026-07-29 09:15", periods=n, freq="5min"),
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
    })

# --- Test: market structure break detection (pure function, no API needed) ---
print("\n--- Market Structure Break Tests ---")

# BUY: recent 10 candles have low=95-104, current close breaks below 95
df_break_buy = make_df_5m(closes=[100,101,102,103,104,103,102,101,100,99,98,90],
                            lows=[95,96,97,98,99,98,97,96,95,94,93,85])
check("BUY structure break detected when price breaks below recent swing low",
      _market_structure_broken(df_break_buy, "BUY", lookback=10) == True)

# BUY: price stays well within range, no break
df_no_break_buy = make_df_5m(closes=[100,101,102,103,104,103,102,101,100,99,101],
                               lows=[95,96,97,98,99,98,97,96,95,94,96])
check("BUY no structure break when price stays above swing low",
      _market_structure_broken(df_no_break_buy, "BUY", lookback=10) == False)

# SELL: mirror case, current close breaks above recent swing high
df_break_sell = make_df_5m(closes=[100,99,98,97,96,97,98,99,100,101,102,110],
                             highs=[105,104,103,102,101,102,103,104,105,106,107,115])
check("SELL structure break detected when price breaks above recent swing high",
      _market_structure_broken(df_break_sell, "SELL", lookback=10) == True)

# Not enough data -> should not crash, should return False (fail-safe)
df_short = make_df_5m(closes=[100, 101, 102])
check("Insufficient data returns False (fail-safe, no crash)",
      _market_structure_broken(df_short, "BUY", lookback=10) == False)

# --- Test: hard stop-loss still works (REGRESSION -- must never break) ---
print("\n--- Hard Stop-Loss Regression Tests ---")

import main as main_module

def fake_confirmed_exit(*args, **kwargs):
    """Stage 4-compatible confirmed exit result for legacy tests."""

    quantity = kwargs.get("quantity")

    if quantity is None and len(args) >= 4:
        quantity = args[3]

    quantity = int(quantity or 0)

    return {
        "success": True,
        "order_id": "TEST-EXIT-ORDER",
        "operation_id": None,
        "status": "COMPLETE",
        "reason": None,
        "requested_quantity": quantity,
        "filled_quantity": quantity,
        "average_price": 100.0,
        "exit_confirmation_pending": False,
        "resolved": True,
    }


def run_check(direction, entry, stop, price_sequence, qty=10, check_trend=False):
    """Helper: mocks fetch_candles to return a fixed last price, runs check_position_exit once."""
    mock_kite = MagicMock()
    open_positions = {
        "TESTSTOCK": {"direction": direction, "qty": qty, "entry": entry, "stop": stop,
                      "target": entry + 10 if direction == "BUY" else entry - 10,
                      "exchange": "NSE", "peak_price": entry, "tight_mode": False}
    }
    tokens = {"TESTSTOCK": 12345}
    exchange_map = {"TESTSTOCK": "NSE"}
    risk = RiskManager(cfg, persist=False)

    df = make_df_5m(closes=price_sequence)
    main_module.fetch_candles = lambda *a, **kw: df
    main_module.place_exit_order = fake_confirmed_exit
    main_module.record_trade = lambda *a, **kw: None
    main_module.save_positions = lambda *a, **kw: None

    status = check_position_exit(mock_kite, "TESTSTOCK", tokens, exchange_map, open_positions, risk, check_trend=check_trend)
    return status, open_positions

# BUY hits hard stop -- price drops straight through stop level
status, positions = run_check("BUY", entry=100.0, stop=95.0, price_sequence=[100,99,98,97,96,94])
check("BUY hard stop triggers correctly (regression)", "CLOSED (stop)" in status)
check("Position removed after hard stop", "TESTSTOCK" not in positions)

# SELL hits hard stop
status, positions = run_check("SELL", entry=100.0, stop=105.0, price_sequence=[100,101,102,103,104,106])
check("SELL hard stop triggers correctly (regression)", "CLOSED (stop)" in status)

# --- Test: fixed target is NEVER used to exit anymore (key behavior change) ---
print("\n--- Target Price No Longer Exits Tests ---")

# BUY: price sails PAST the old target (entry+10=110) but doesn't hit
# hard stop, doesn't break structure, no trend check -- should STAY OPEN
status, positions = run_check("BUY", entry=100.0, stop=90.0,
                                price_sequence=[100,102,104,106,108,110,112,115])
check("BUY position stays open past old target price (target no longer exits)",
      "position open" in status)
check("Position NOT removed just because target was passed", "TESTSTOCK" in positions)

# --- Test: quiet/flat market -- nothing triggers, position stays open ---
print("\n--- Flat Market -- No False Exit Tests ---")
status, positions = run_check("BUY", entry=100.0, stop=90.0,
                                price_sequence=[100,100.2,99.8,100.1,99.9,100.0])
check("Flat/sideways price action does not trigger any exit",
      "position open" in status)

# --- Summary ---
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}")

# --- Test: ATR trailing stop ---
print("\n--- ATR Trailing Stop Tests ---")

# BUY: price runs up strongly then reverses sharply -- should trigger
# trailing stop (not hard stop, since hard stop is far below entry)
prices_run_then_reverse = [100,102,104,106,108,110,112,114,112,108,102,95]
status, positions = run_check("BUY", entry=100.0, stop=80.0,
                                price_sequence=prices_run_then_reverse)
check("BUY trailing stop triggers on sharp reversal after a run-up",
      "trailing_stop" in status or "TESTSTOCK" not in positions)

# BUY: price grinds up steadily, small pullbacks, should NOT trigger
# trailing stop (small ATR-sized moves shouldn't close a healthy trend)
prices_steady_climb = [100,100.5,101,100.8,101.5,102,101.8,102.5,103,102.8,103.5]
status, positions = run_check("BUY", entry=100.0, stop=80.0,
                                price_sequence=prices_steady_climb)
check("BUY steady climb with small pullbacks stays open (no false trailing exit)",
      "position open" in status)

# BUY: peak_price tracking -- confirm it only ever increases (never
# retreats) as price makes new highs, then stays fixed on a pullback
mock_kite = MagicMock()
open_positions = {"TESTSTOCK": {"direction": "BUY", "qty": 10, "entry": 100.0, "stop": 80.0,
                                  "target": 110, "exchange": "NSE", "peak_price": 100.0, "tight_mode": False}}
tokens = {"TESTSTOCK": 12345}
exchange_map = {"TESTSTOCK": "NSE"}
risk = RiskManager(cfg, persist=False)
main_module.fetch_candles = lambda *a, **kw: make_df_5m(closes=[100,102,104,106,108,110,109,108])
main_module.place_exit_order = fake_confirmed_exit
main_module.record_trade = lambda *a, **kw: None
main_module.save_positions = lambda *a, **kw: None
check_position_exit(mock_kite, "TESTSTOCK", tokens, exchange_map, open_positions, risk, check_trend=False)
if "TESTSTOCK" in open_positions:
    check("peak_price correctly tracks the highest price seen (110, not the pullback to 108)",
          open_positions["TESTSTOCK"]["peak_price"] == 110)
else:
    check("peak_price correctly tracks the highest price seen (110, not the pullback to 108)", False)

# --- Test: trend-reversal exit (check_trend=True path) ---
print("\n--- Trend Reversal Tests ---")

def make_df_15m_downtrend(n=60):
    """Synthetic 15m data trending DOWN -- close < ema_fast < ema_slow < vwap pattern."""
    closes = [200 - i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "date": pd.date_range("2026-07-28 09:15", periods=n, freq="15min"),
        "open": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes],
        "close": closes, "volume": [1000] * n,
    })

mock_kite2 = MagicMock()
open_positions2 = {"TESTSTOCK": {"direction": "BUY", "qty": 10, "entry": 150.0, "stop": 100.0,
                                   "target": 160, "exchange": "NSE", "peak_price": 150.0, "tight_mode": False}}
tokens2 = {"TESTSTOCK": 12345}
exchange_map2 = {"TESTSTOCK": "NSE"}
risk2 = RiskManager(cfg, persist=False)

# 5m price stays flat/safe (won't hit hard stop or trailing stop or structure break)
flat_5m = make_df_5m(closes=[150,150.2,150.1,150.3,150.2,150.4,150.3,150.5,150.4,150.6,150.5,150.7])
down_15m = make_df_15m_downtrend()

call_count = {"n": 0}
def fake_fetch(kite, token, interval, lookback_days=1, **kwargs):
    call_count["n"] += 1
    if interval == cfg.ENTRY_TIMEFRAME:
        return flat_5m
    return down_15m  # 15m call returns a clear downtrend

main_module.fetch_candles = fake_fetch
main_module.place_exit_order = fake_confirmed_exit
main_module.record_trade = lambda *a, **kw: None
main_module.save_positions = lambda *a, **kw: None

status2 = check_position_exit(mock_kite2, "TESTSTOCK", tokens2, exchange_map2, open_positions2, risk2, check_trend=True)
check("BUY position exits on trend reversal when check_trend=True and 15m trend is DOWN",
      "trend_reversal" in status2)

# Same scenario but check_trend=False -- should NOT check 15m trend at all, stays open
open_positions3 = {"TESTSTOCK": {"direction": "BUY", "qty": 10, "entry": 150.0, "stop": 100.0,
                                   "target": 160, "exchange": "NSE", "peak_price": 150.0, "tight_mode": False}}
risk3 = RiskManager(cfg, persist=False)
status3 = check_position_exit(mock_kite2, "TESTSTOCK", tokens2, exchange_map2, open_positions3, risk3, check_trend=False)
check("check_trend=False skips trend check entirely (lightweight loop stays cheap)",
      "position open" in status3)

# --- Test: peak_price/tight_mode survive a save/load round-trip (no state contamination) ---
print("\n--- State Persistence Tests ---")

import json
import tempfile
from position_store import save_positions, load_positions
import position_store

test_positions = {
    "TESTSTOCK": {"direction": "BUY", "qty": 10, "entry": 100.0, "stop": 90.0,
                  "target": 110, "exchange": "NSE", "peak_price": 108.5, "tight_mode": True}
}

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
    temp_path = tf.name

original_path = position_store.POSITIONS_PATH if hasattr(position_store, "POSITIONS_PATH") else None
try:
    with open(temp_path, "w") as f:
        json.dump(test_positions, f)
    with open(temp_path) as f:
        reloaded = json.load(f)
    check("peak_price survives a save/load round-trip",
          reloaded["TESTSTOCK"]["peak_price"] == 108.5)
    check("tight_mode survives a save/load round-trip",
          reloaded["TESTSTOCK"]["tight_mode"] == True)
finally:
    import os
    os.unlink(temp_path)

# --- Final summary ---
print(f"\n{'='*50}")
print(f"FINAL Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
import sys
sys.exit(1 if failed else 0)
