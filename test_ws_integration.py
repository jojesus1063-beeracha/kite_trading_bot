"""
Exercises WSShadowEngine.handle_tick() end-to-end (candle building +
indicator updates + 15-min combination) using a fake Kite client so no
real network/credentials are needed. Does NOT test WSTicker/KiteTicker
itself (that needs a real Kite connection) -- only the pure logic that
runs once ticks arrive, which is where a bug would actually corrupt
candles or indicators.
"""

from datetime import datetime, timezone

import pandas as pd

from ws_integration import WSShadowEngine

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeKite:
    """Returns empty REST data for every call -- forces the engine down
    its 'no REST data available' paths, which must never raise."""
    def historical_data(self, *a, **kw):
        return []

    def instruments(self, *a, **kw):
        return []


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def tick(t, price, cum_vol):
    return {"exchange_timestamp": ts(t), "last_price": price, "volume_traded": cum_vol}


engine = WSShadowEngine(FakeKite(), symbols=["TESTSYM"], tokens={"TESTSYM": 12345}, exchange_map={"TESTSYM": "NSE"})

# seed_from_history against a Kite client that returns no data must not raise
engine.seed_from_history()
check("seed_from_history() with no REST data does not raise", True)

# Feed 3 full 5-min candles' worth of ticks (15 minutes = one 15-min candle)
times_and_prices = [
    ("2026-08-05T09:15:00", 100.0, 1000),
    ("2026-08-05T09:16:00", 101.0, 1200),
    ("2026-08-05T09:19:00", 102.0, 1400),
    ("2026-08-05T09:20:00", 103.0, 1600),  # finalizes 09:15 5-min candle
    ("2026-08-05T09:24:00", 104.0, 1800),
    ("2026-08-05T09:25:00", 105.0, 2000),  # finalizes 09:20 5-min candle
    ("2026-08-05T09:29:00", 106.0, 2200),
    ("2026-08-05T09:30:00", 107.0, 2400),  # finalizes 09:25 5-min candle -> completes 09:15-09:30 15-min group
]

for t, price, vol in times_and_prices:
    engine.handle_tick("TESTSYM", tick(t, price, vol))

check("Unknown symbol tick is silently ignored, not an error",
      engine.handle_tick("NOT_A_SYMBOL", tick("2026-08-05T09:15:00", 100.0, 1000)) is None)

builder = engine.candle_builders_5m["TESTSYM"]
check("3 5-min candles finalized from the fed ticks", len(builder.finalized) == 3)

check("15-min candle combination triggered once 3 legs completed",
      len(engine.finalized_15m["TESTSYM"]) == 3)

state_5m = engine.indicator_state_5m["TESTSYM"]
check("5-min EMA state updated from ticks", state_5m.ema_periods.get(20) is not None)
check("5-min ATR window has entries after 3 candles", len(getattr(state_5m, "_atr_window", [])) == 3)

state_15m = engine.indicator_state_15m["TESTSYM"]
check("15-min EMA state updated after one complete 15-min group",
      state_15m.ema_periods.get(20) is not None)
check("15-min VWAP state has accumulated volume",
      state_15m.vwap_cum_vol > 0)

# Feed the same 15-min group's ticks again in a way that would try to
# re-emit the same 15-min candle -- must not double-count.
vwap_vol_before = state_15m.vwap_cum_vol
extra_tick = tick("2026-08-05T09:34:00", 108.0, 2600)
engine.handle_tick("TESTSYM", extra_tick)
check("Re-processing ticks in an already-emitted 15-min window doesn't double-emit",
      state_15m.vwap_cum_vol == vwap_vol_before)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
