from datetime import datetime, timezone

from candle_engine import SymbolCandleBuilder


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


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def tick(value, price, cumulative_volume):
    return {
        "exchange_timestamp": ts(value),
        "last_price": price,
        "volume_traded": cumulative_volume,
    }


# Regression: a WS process that connects at 09:32 cannot reconstruct the
# missing 09:30-09:32 portion of the candle. That startup-partial candle must
# never be finalized/compared/consumed as if it represented the full interval.
builder = SymbolCandleBuilder("MIDSTART", interval_minutes=5)
builder.add_tick(tick("2026-08-07T09:32:00", 100.0, 1500))
builder.add_tick(tick("2026-08-07T09:34:30", 101.0, 1800))
first_rollover = builder.add_tick(tick("2026-08-07T09:35:00", 102.0, 1900))

check("mid-interval startup candle is discarded at first rollover", first_rollover is None)
check("startup-partial candle is not stored as finalized", len(builder.finalized) == 0)

# After the first observed boundary the builder is synchronized. The 09:35
# candle is complete and should finalize normally at 09:40.
builder.add_tick(tick("2026-08-07T09:36:00", 103.0, 2200))
builder.add_tick(tick("2026-08-07T09:39:30", 101.5, 2400))
second_rollover = builder.add_tick(tick("2026-08-07T09:40:00", 104.0, 2500))

check("first fully observed candle finalizes normally", second_rollover is not None)
check("first fully observed candle starts at 09:35", second_rollover["date"] == ts("2026-08-07T09:35:00"))
check("first fully observed candle open is first 09:35 tick", second_rollover["open"] == 102.0)
check("first fully observed candle close is last pre-09:40 tick", second_rollover["close"] == 101.5)
check("first fully observed candle high/low are built from observed ticks", second_rollover["high"] == 103.0 and second_rollover["low"] == 101.5)

# Critical volume regression: old code reset _last_cum_volume at every candle
# boundary, losing the cumulative-volume delta from the last old-candle tick
# to the first new-candle tick. The synchronized 09:35 candle must carry the
# 1800 baseline across the boundary, therefore volume through 09:39:30 is
# 2400 - 1800 = 600.
check("cumulative volume baseline is carried across interval boundary", second_rollover["volume"] == 600)

# Existing valid path remains valid: if the first observed tick is exactly on
# the interval boundary, the first candle is complete and is not discarded.
boundary_builder = SymbolCandleBuilder("BOUNDARY", interval_minutes=5)
boundary_builder.add_tick(tick("2026-08-07T10:00:00", 200.0, 5000))
boundary_builder.add_tick(tick("2026-08-07T10:04:30", 201.0, 5600))
boundary_closed = boundary_builder.add_tick(tick("2026-08-07T10:05:00", 202.0, 5700))

check("exact-boundary startup still emits first complete candle", boundary_closed is not None)
check("exact-boundary candle retains prior volume behavior", boundary_closed["volume"] == 600)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
