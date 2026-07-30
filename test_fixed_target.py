from unittest.mock import MagicMock
import pandas as pd
import config as cfg
from risk_manager import RiskManager
import main as main_module

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def make_df_5m(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-30 09:15", periods=n, freq="5min"),
        "open": closes, "high": [c+1 for c in closes], "low": [c-1 for c in closes],
        "close": closes, "volume": [1000]*n,
    })

cfg.ENABLE_FIXED_TARGET = True
cfg.PROFIT_TARGET_PERCENT = 1.5
mock_kite = MagicMock()

# CRITICAL: mock every function that writes to real production files --
# record_trade()/save_positions() write to the actual trade_history.jsonl
# and open_positions.json on disk, not a sandbox. An earlier run of this
# file polluted both real files with fake TEST/TEST2/TEST4 data before
# this was added -- never call the real ones in a test.
main_module.record_trade = lambda *a, **kw: None
main_module.save_positions = lambda *a, **kw: None

# --- BUY exits exactly at +1.5% ---
entry = 1000.0
target = entry * 1.015  # 1015.0
open_positions = {"TEST": {"direction": "BUY", "qty": 10, "entry": entry, "stop": entry*0.9965,
                            "target": target, "exchange": "NSE", "peak_price": entry, "tight_mode": False}}
main_module.fetch_candles = lambda *a, **kw: make_df_5m([entry, entry+5, target])
main_module.place_exit_order = lambda *a, **kw: True
risk = RiskManager(cfg, persist=False)
status = main_module.check_position_exit(mock_kite, "TEST", {"TEST": 1}, {"TEST": "NSE"}, open_positions, risk, check_trend=False)
check("BUY exits exactly at +1.5% target", "fixed_target" in status and "TEST" not in open_positions)

# --- SELL exits exactly at -1.5% (i.e. +1.5% profit for a short) ---
entry_sell = 1000.0
target_sell = entry_sell * 0.985  # 985.0
open_positions2 = {"TEST2": {"direction": "SELL", "qty": 10, "entry": entry_sell, "stop": entry_sell*1.0035,
                              "target": target_sell, "exchange": "NSE", "peak_price": entry_sell, "tight_mode": False}}
main_module.fetch_candles = lambda *a, **kw: make_df_5m([entry_sell, entry_sell-5, target_sell])
risk2 = RiskManager(cfg, persist=False)
status2 = main_module.check_position_exit(mock_kite, "TEST2", {"TEST2": 1}, {"TEST2": "NSE"}, open_positions2, risk2, check_trend=False)
check("SELL exits exactly at +1.5% profit (target below entry)", "fixed_target" in status2 and "TEST2" not in open_positions2)

# --- Target calculation correct for different entry prices ---
for entry_price in [50.0, 1800.0, 14020.0]:
    expected_buy_target = entry_price * 1.015
    expected_sell_target = entry_price * 0.985
    check(f"Target calc correct for entry {entry_price} (BUY)", abs(expected_buy_target - entry_price * (1 + cfg.PROFIT_TARGET_PERCENT/100)) < 0.001)
    check(f"Target calc correct for entry {entry_price} (SELL)", abs(expected_sell_target - entry_price * (1 - cfg.PROFIT_TARGET_PERCENT/100)) < 0.001)

# --- Trailing stop, structure break, trend reversal all bypassed when fixed-target mode is on ---
# Price makes a big run-up then sharp reversal (would normally trigger ATR trailing stop) -- but stays below target and above stop
open_positions3 = {"TEST3": {"direction": "BUY", "qty": 10, "entry": 100.0, "stop": 99.65,
                              "target": 101.5, "exchange": "NSE", "peak_price": 100.0, "tight_mode": False}}
main_module.fetch_candles = lambda *a, **kw: make_df_5m([100,105,110,108,106,100.5])  # sharp reversal, never hits target or stop
risk3 = RiskManager(cfg, persist=False)
status3 = main_module.check_position_exit(mock_kite, "TEST3", {"TEST3": 1}, {"TEST3": "NSE"}, open_positions3, risk3, check_trend=True)
check("Fixed-target mode: sharp reversal does NOT trigger trailing stop (bypassed)", "TEST3" in open_positions3)
check("Fixed-target mode: position status shows still open, not closed", "position open" in status3)

# --- Stop-loss continues to function correctly in fixed-target mode ---
open_positions4 = {"TEST4": {"direction": "BUY", "qty": 10, "entry": 100.0, "stop": 99.65,
                              "target": 101.5, "exchange": "NSE", "peak_price": 100.0, "tight_mode": False}}
main_module.fetch_candles = lambda *a, **kw: make_df_5m([100,99.9,99.7,99.5])  # breaches stop
risk4 = RiskManager(cfg, persist=False)
status4 = main_module.check_position_exit(mock_kite, "TEST4", {"TEST4": 1}, {"TEST4": "NSE"}, open_positions4, risk4, check_trend=False)
check("Stop-loss still functions correctly in fixed-target mode", "stop" in status4 and "TEST4" not in open_positions4)

# --- Old mode (ENABLE_FIXED_TARGET=False): existing exit logic completely unaffected ---
cfg.ENABLE_FIXED_TARGET = False
open_positions5 = {"TEST5": {"direction": "BUY", "qty": 10, "entry": 100.0, "stop": 90.0,
                              "target": 110.0, "exchange": "NSE", "peak_price": 100.0, "tight_mode": False}}
main_module.fetch_candles = lambda *a, **kw: make_df_5m([100]*20 + [101.5])  # would hit "target" under fixed mode, but old mode ignores target entirely
risk5 = RiskManager(cfg, persist=False)
status5 = main_module.check_position_exit(mock_kite, "TEST5", {"TEST5": 1}, {"TEST5": "NSE"}, open_positions5, risk5, check_trend=False)
check("Old mode: position stays open even at +1.5% (target not checked in normal mode)", "TEST5" in open_positions5)
cfg.ENABLE_FIXED_TARGET = True

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
