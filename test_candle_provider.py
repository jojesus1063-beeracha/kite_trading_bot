import pandas as pd

from candle_provider import augment_with_ws, _interval_to_timeframe_label

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeEngineAugments:
    """Simulates a WSShadowEngine that successfully augments."""
    def get_augmented_candles(self, symbol, timeframe_label, df):
        new_row = pd.DataFrame([{"date": pd.Timestamp("2026-08-05 09:35:00"),
                                  "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}])
        return pd.concat([df, new_row], ignore_index=True), True


class FakeEngineDeclines:
    """Simulates a WSShadowEngine that has nothing confident to add."""
    def get_augmented_candles(self, symbol, timeframe_label, df):
        return df, False


class FakeEngineRaises:
    """Simulates a WSShadowEngine that errors -- provider must not propagate this."""
    def get_augmented_candles(self, symbol, timeframe_label, df):
        raise RuntimeError("simulated failure")


base_df = pd.DataFrame([{"date": pd.Timestamp("2026-08-05 09:30:00"),
                          "open": 1, "high": 1, "low": 1, "close": 1, "volume": 100}])


class FakeCfgOff:
    ENABLE_WS_CANDLES = False
    WS_CANDLE_MODE = "shadow"


class FakeCfgShadow:
    ENABLE_WS_CANDLES = True
    WS_CANDLE_MODE = "shadow"


class FakeCfgLive:
    ENABLE_WS_CANDLES = True
    WS_CANDLE_MODE = "live"


import sys
import config as real_cfg_module

def with_fake_config(fake_cfg_cls, fn):
    """Temporarily monkeypatches the 'config' module attributes candle_provider reads."""
    saved = {}
    for attr in ("ENABLE_WS_CANDLES", "WS_CANDLE_MODE"):
        saved[attr] = getattr(real_cfg_module, attr, None)
        setattr(real_cfg_module, attr, getattr(fake_cfg_cls, attr))
    try:
        return fn()
    finally:
        for attr, val in saved.items():
            setattr(real_cfg_module, attr, val)


# -- ws_engine=None -> always REST-only, regardless of config ------------

result = augment_with_ws(base_df, symbol="TEST", interval="5minute", ws_engine=None)
check("ws_engine=None returns df unmodified", result.equals(base_df))

# -- ENABLE_WS_CANDLES=False -> never augments even with a working engine -

result = with_fake_config(FakeCfgOff, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="5minute", ws_engine=FakeEngineAugments()))
check("ENABLE_WS_CANDLES=False -> not augmented even though engine would succeed",
      len(result) == len(base_df))

# -- WS_CANDLE_MODE='shadow' -> never augments (only 'live' does) --------

result = with_fake_config(FakeCfgShadow, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="5minute", ws_engine=FakeEngineAugments()))
check("WS_CANDLE_MODE='shadow' -> not augmented even though engine would succeed",
      len(result) == len(base_df))

# -- live mode, engine augments -> result actually grows ------------------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="5minute", ws_engine=FakeEngineAugments()))
check("live mode + engine augments -> df grows by one row", len(result) == len(base_df) + 1)

# -- live mode, engine declines -> unchanged -------------------------------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="5minute", ws_engine=FakeEngineDeclines()))
check("live mode + engine declines -> df unchanged", len(result) == len(base_df))

# -- live mode, engine raises -> fails safe, returns original df ----------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="5minute", ws_engine=FakeEngineRaises()))
check("live mode + engine raises an exception -> fails safe, df unchanged, no propagation",
      result.equals(base_df))

# -- unknown interval string -> declines gracefully ------------------------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    base_df, symbol="TEST", interval="60minute", ws_engine=FakeEngineAugments()))
check("Unrecognized interval string -> returns df unmodified, no crash",
      len(result) == len(base_df))

check("_interval_to_timeframe_label maps '5minute' correctly",
      _interval_to_timeframe_label("5minute") == "5minute")
check("_interval_to_timeframe_label maps '15minute' correctly",
      _interval_to_timeframe_label("15minute") == "15minute")
check("_interval_to_timeframe_label returns None for unknown intervals",
      _interval_to_timeframe_label("day") is None)

# -- df=None -> handled gracefully -----------------------------------------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    None, symbol="TEST", interval="5minute", ws_engine=FakeEngineAugments()))
check("df=None input -> returns None, no crash", result is None)

# -- symbol=None -> treated same as ws_engine=None (REST-only) -----------

result = with_fake_config(FakeCfgLive, lambda: augment_with_ws(
    base_df, symbol=None, interval="5minute", ws_engine=FakeEngineAugments()))
check("symbol=None -> returns df unmodified (can't look up engine state without a symbol)",
      len(result) == len(base_df))

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
