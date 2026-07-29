from unittest.mock import MagicMock
import pandas as pd
import config as cfg
from strategy import Signal
import main as main_module
from risk_manager import RiskManager

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

def run_scan_for_alignment(direction, market_trend_val, sector_trend_val, filter_enabled):
    def fake_evaluate(symbol, df15, df5, cfg_arg):
        return Signal(symbol, direction, 100.0, 95.0, 110.0, pd.Timestamp("2026-07-29 10:00"), "test signal")

    main_module.evaluate = fake_evaluate
    main_module.fetch_candles = lambda *a, **kw: pd.DataFrame({
        "date": pd.date_range("2026-07-29 09:15", periods=60, freq="15min"),
        "open": [100]*60, "high": [101]*60, "low": [99]*60, "close": [100]*60, "volume": [1000]*60,
    })
    main_module.get_market_trend = lambda kite, cfg_arg: market_trend_val
    main_module.get_sector_trend = lambda kite, symbol, cfg_arg: sector_trend_val
    main_module.place_entry_order = lambda *a, **kw: {"success": True, "order_id": "TEST", "status": "SUBMITTED", "reason": None}
    main_module.log_signal = lambda record: True
    main_module.save_positions = lambda *a, **kw: None
    main_module.within_trading_window = lambda: True

    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = filter_enabled

    mock_kite = MagicMock()
    tokens = {"TEST": 12345}
    exchange_map = {"TEST": "NSE"}
    open_positions = {}
    risk = RiskManager(cfg, persist=False)

    status = main_module.run_full_scan(mock_kite, ["TEST"], tokens, exchange_map, open_positions, risk)
    return status[0]["status"], open_positions

# Filter DISABLED (default) -- MISALIGNED signal should still execute
status, positions = run_scan_for_alignment("SELL", "Bullish", "Bullish", filter_enabled=False)
check("Filter disabled: MISALIGNED SELL-vs-bullish signal still executes (backward compatible)",
      "ENTRY" in status and "TEST" in positions)

# Filter ENABLED -- MISALIGNED signal should be skipped
status, positions = run_scan_for_alignment("SELL", "Bullish", "Bullish", filter_enabled=True)
check("Filter enabled: SELL vs Bullish market+sector (STRONG_MISALIGNMENT) is skipped",
      "skipped, misaligned" in status and "TEST" not in positions)

# Filter ENABLED -- ALIGNED signal should still execute
status, positions = run_scan_for_alignment("BUY", "Bullish", "Bullish", filter_enabled=True)
check("Filter enabled: BUY vs Bullish market+sector (STRONG_ALIGNMENT) still executes",
      "ENTRY" in status and "TEST" in positions)

# Filter ENABLED -- NEUTRAL signal should still execute
status, positions = run_scan_for_alignment("BUY", "Sideways", "Sideways", filter_enabled=True)
check("Filter enabled: NEUTRAL alignment still executes (not blocked)",
      "ENTRY" in status and "TEST" in positions)

cfg.ENABLE_MARKET_ALIGNMENT_FILTER = False  # restore safe default

print()
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
