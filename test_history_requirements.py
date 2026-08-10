from pathlib import Path

from history_requirements import (
    entry_indicator_lookback_days,
    entry_trend_lookback_days,
)


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


class DisabledCfg:
    ENABLE_200_EMA_FILTER = False
    ENABLE_EMA200_WATCHLIST = False


class ProductionEmaCfg:
    ENABLE_200_EMA_FILTER = True
    ENABLE_EMA200_WATCHLIST = True
    EMA200_PERIOD = 200
    EMA200_SLOPE_LOOKBACK = 5
    EMA200_LOOKBACK = 250
    EMA200_HISTORY_LOOKBACK_DAYS = 20


class LargerEmaCfg(ProductionEmaCfg):
    EMA200_PERIOD = 400
    EMA200_LOOKBACK = 450
    EMA200_HISTORY_LOOKBACK_DAYS = 20


class ThreeMinuteEntryCfg:
    ENTRY_TIMEFRAME = "3minute"
    ENTRY_HISTORY_LOOKBACK_DAYS = 5
    ENTRY_EMA = 20
    VOLUME_LOOKBACK = 20
    RVOL_LOOKBACK = 20


check(
    "EMA gates disabled retain the five-day trend fetch",
    entry_trend_lookback_days(DisabledCfg) == 5,
)
check(
    "Three-minute entry indicators retain at least five calendar days",
    entry_indicator_lookback_days(ThreeMinuteEntryCfg) == 5,
)
check(
    "Production EMA200 settings request twenty calendar days",
    entry_trend_lookback_days(ProductionEmaCfg) == 20,
)
check(
    "Derived history expands automatically when the EMA requirement grows",
    entry_trend_lookback_days(LargerEmaCfg) > 20,
)

main_source = Path("main.py").read_text()
check(
    "Aligned entry fetch uses the derived trend-history requirement",
    main_source.count("lookback_days=trend_lookback_days") == 2,
)
check(
    "Aligned entry fetch uses derived entry-indicator history",
    main_source.count("lookback_days=entry_lookback_days") == 2,
)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
