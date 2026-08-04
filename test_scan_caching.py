"""
Deterministic (mocked) tests for Step 4c's per-scan caching behavior --
does not depend on live market conditions or real signals firing.
Run: python3 test_scan_caching.py
"""
from unittest.mock import MagicMock, patch
import pandas as pd
import config as cfg
from risk_manager import RiskManager
from strategy import Signal
import main as main_module

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"PASS: {name}")
        passed += 1
    else:
        print(f"FAIL: {name}")
        failed += 1

# Force evaluate() to always return a real signal, so the alignment/
# caching branch definitely executes for every symbol.
def fake_evaluate(symbol, df_15m, df_5m, cfg_arg):
    return Signal(symbol, "BUY", 100.0, 95.0, 110.0, pd.Timestamp("2026-07-29 10:00"), "test signal")

main_module.evaluate = fake_evaluate
main_module.fetch_candles = lambda *a, **kw: pd.DataFrame({
    "date": pd.date_range("2026-07-29 09:15", periods=60, freq="15min"),
    "open": [100]*60, "high": [101]*60, "low": [99]*60, "close": [100]*60, "volume": [1000]*60,
})
main_module.place_entry_order = lambda *a, **kw: {"success": False, "order_id": None, "status": "REJECTED", "reason": "test mock"}  # matches new contract; order intentionally "fails" so we do not need open_positions bookkeeping
main_module.risk_manager_within_trading_window = None

mock_kite = MagicMock()

fetch_call_log = []
def fake_get_market_trend(kite, cfg_arg):
    fetch_call_log.append("nifty")
    return "Bullish", "OK"

def fake_get_sector_trend(kite, symbol, cfg_arg):
    sector = main_module.sector_for_symbol(symbol)
    fetch_call_log.append(f"sector:{sector}")
    return "Bullish", "OK"

main_module.get_market_trend_diagnostic = fake_get_market_trend
main_module.log_signal = lambda record: True  # isolate from real signal_logs/ during tests
main_module.get_sector_trend_diagnostic = fake_get_sector_trend
candidate_events = []
main_module.record_validation_event = (
    lambda event_type, payload: candidate_events.append(
        (event_type, payload)
    )
)

# 3 bank stocks (share ONE sector) + 1 IT stock (separate sector)
test_symbols = ["HDFCBANK", "ICICIBANK", "AXISBANK", "TCS"]
tokens = {s: 12345 for s in test_symbols}
exchange_map = {s: "NSE" for s in test_symbols}
open_positions = {}
risk = RiskManager(cfg, persist=False)

original_within = main_module.within_trading_window
main_module.within_trading_window = lambda: True

status = main_module.run_full_scan(mock_kite, test_symbols, tokens, exchange_map, open_positions, risk)

main_module.within_trading_window = original_within

check("Nifty trend fetched exactly ONCE for 4 symbols", fetch_call_log.count("nifty") == 1)
check("Bank sector fetched exactly ONCE despite 3 bank stocks",
      fetch_call_log.count("sector:NIFTY BANK") == 1)
check("IT sector fetched exactly ONCE for TCS",
      fetch_call_log.count("sector:NIFTY IT") == 1)
check("Total fetches = 1 nifty + 2 sectors = 3 (not 4 sector fetches, proving cache worked)",
      len(fetch_call_log) == 3)
check(
    "Sector diagnostic reason survives cache hits",
    len([
        event
        for event in candidate_events
        if event[0] == "candidate_collected"
    ]) == 4
    and all(
        payload.get("sector_trend_reason") == "OK"
        for event_type, payload in candidate_events
        if event_type == "candidate_collected"
    ),
)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
import sys
sys.exit(1 if failed else 0)
