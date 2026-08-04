from unittest.mock import MagicMock

import pandas as pd

import config as cfg
import main as main_module
from risk_manager import RiskManager
from strategy import Signal


passed = 0
failed = 0


def check(name, condition):
    global passed, failed

    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


def run_scan_for_alignment(
    direction,
    market_trend_val,
    sector_trend_val,
    filter_enabled,
):
    """
    Run one mocked full scan with a selected market/sector alignment.
    """

    def fake_evaluate(symbol, df15, df5, cfg_arg):
        return Signal(
            symbol,
            direction,
            100.0,
            95.0,
            110.0,
            pd.Timestamp("2026-07-29 10:00"),
            "test signal",
        )

    # Mock candle data.
    main_module.evaluate = fake_evaluate
    main_module.fetch_candles = lambda *args, **kwargs: pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-07-29 09:15",
                periods=60,
                freq="15min",
            ),
            "open": [100] * 60,
            "high": [101] * 60,
            "low": [99] * 60,
            "close": [100] * 60,
            "volume": [1000] * 60,
        }
    )

    # Mock market and sector trends.
    main_module.get_market_trend = (
        lambda kite, cfg_arg: market_trend_val
    )

    main_module.get_sector_trend = (
        lambda kite, symbol, cfg_arg: sector_trend_val
    )
    main_module.sector_for_symbol = lambda symbol: "NIFTY TEST"

    # Stage 3-compatible confirmed entry result.
    # The old test returned only "SUBMITTED", which no longer contains
    # the confirmed filled quantity required by main.py.
    def fake_place_entry_order(
        kite,
        symbol,
        entry_direction,
        quantity,
        exchange,
        cfg_arg,
        entry_plan=None,
    ):
        return {
            "success": True,
            "order_id": "TEST-ORDER",
            "operation_id": "TEST-OPERATION",
            "status": "COMPLETE",
            "reason": None,
            "status_message": None,
            "requested_quantity": quantity,
            "filled_quantity": quantity,
            "pending_quantity": 0,
            "cancelled_quantity": 0,
            "average_price": 100.0,
            "terminal": True,
            "confirmation_pending": False,
        }

    main_module.place_entry_order = fake_place_entry_order
    main_module.log_signal = lambda record: True
    main_module.save_positions = lambda *args, **kwargs: None
    main_module.within_trading_window = lambda: True

    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = filter_enabled

    mock_kite = MagicMock()

    tokens = {
        "TEST": 12345,
    }

    exchange_map = {
        "TEST": "NSE",
    }

    open_positions = {}

    risk = RiskManager(
        cfg,
        persist=False,
    )

    status = main_module.run_full_scan(
        mock_kite,
        ["TEST"],
        tokens,
        exchange_map,
        open_positions,
        risk,
    )

    return status[0]["status"], open_positions


# 1. Filter disabled:
# A misaligned SELL signal should still execute.
status, positions = run_scan_for_alignment(
    "SELL",
    "Bullish",
    "Bullish",
    filter_enabled=False,
)

check(
    "Filter disabled: MISALIGNED SELL-vs-Bullish signal still executes",
    "ENTRY" in status and "TEST" in positions,
)


# 2. Filter enabled:
# A strongly misaligned SELL signal should be blocked.
status, positions = run_scan_for_alignment(
    "SELL",
    "Bullish",
    "Bullish",
    filter_enabled=True,
)

check(
    "Filter enabled: SELL vs Bullish market and sector is skipped",
    "skipped" in status.lower()
    and "misaligned" in status.lower()
    and "TEST" not in positions,
)


# 3. Filter enabled:
# A strongly aligned BUY signal should execute.
status, positions = run_scan_for_alignment(
    "BUY",
    "Bullish",
    "Bullish",
    filter_enabled=True,
)

check(
    "Filter enabled: BUY vs Bullish market and sector still executes",
    "ENTRY" in status and "TEST" in positions,
)


# 4. Filter enabled:
# A neutral signal should not be blocked.
status, positions = run_scan_for_alignment(
    "BUY",
    "Sideways",
    "Sideways",
    filter_enabled=True,
)

check('Filter enabled: NEUTRAL alignment is blocked', not ("ENTRY" in status and "TEST" in positions))


# Restore the normal safe default after the test.
cfg.ENABLE_MARKET_ALIGNMENT_FILTER = False


print()
print(
    "Results: "
    + str(passed)
    + " passed, "
    + str(failed)
    + " failed"
)

if failed:
    raise SystemExit(1)
