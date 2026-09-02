from types import SimpleNamespace

import pandas as pd

from ws_integration import WSShadowEngine


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


def engine_with(latest, validated):
    engine = WSShadowEngine.__new__(WSShadowEngine)
    engine.entry_interval = "3minute"
    engine.entry_interval_minutes = 3
    engine.candle_builders_entry = {
        "TEST": SimpleNamespace(finalized=[latest])
    }
    engine._last_finalized_15m = {}
    engine._validated_entry_dates = {"TEST": set(validated)}
    engine._augmentation_count = {}
    engine._augmentation_skip_count = {}
    return engine


latest_date = pd.Timestamp.now().floor("3min") - pd.Timedelta(minutes=3)
latest = {
    "date": latest_date,
    "open": 100.0,
    "high": 102.0,
    "low": 99.0,
    "close": 101.0,
    "volume": 1200.0,
}
rest = pd.DataFrame(
    [
        {
            "date": latest_date - pd.Timedelta(minutes=3),
            "open": 99.0,
            "high": 101.0,
            "low": 98.0,
            "close": 100.0,
            "volume": 1000.0,
        }
    ]
)

unvalidated = engine_with(latest, validated=[])
result, augmented = unvalidated.get_augmented_candles(
    "TEST", "3minute", rest
)
check(
    "Unvalidated WS candle cannot influence strategy data",
    augmented is False and result.equals(rest),
)

validated = engine_with(
    latest,
    validated=[WSShadowEngine._candle_key(latest_date)],
)
result, augmented = validated.get_augmented_candles(
    "TEST", "3minute", rest
)
check(
    "REST-tolerance-validated contiguous WS candle can augment",
    augmented is True and len(result) == len(rest) + 1,
)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
