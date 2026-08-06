import pandas as pd
from datetime import datetime, timedelta

from rvol import compute_rvol, passes_rvol_threshold, NOT_ENABLED, OK, format_rvol_log

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeCfg:
    ENABLE_RVOL_FILTER = True
    RVOL_LOOKBACK = 20
    RVOL_THRESHOLD = 1.5


def make_df(volumes):
    base = datetime(2026, 8, 6, 9, 15)
    rows = []
    for i, v in enumerate(volumes):
        rows.append({"date": base + timedelta(minutes=5 * i), "open": 100, "high": 101, "low": 99,
                     "close": 100, "volume": v})
    return pd.DataFrame(rows)


# -- Disabled: never touches data ------------------------------------------

cfg_off = FakeCfg()
cfg_off.ENABLE_RVOL_FILTER = False
status, rvol, detail = compute_rvol(None, cfg_off)
check("Disabled -> NOT_ENABLED even with df=None", status == NOT_ENABLED)
check("Disabled -> rvol value is None", rvol is None)

passes, rvol, detail = passes_rvol_threshold(None, cfg_off)
check("Disabled -> passes_rvol_threshold returns True unconditionally (never blocks)", passes is True)

# -- Insufficient data ------------------------------------------------------

cfg = FakeCfg()
short_df = make_df([1000] * 10)  # fewer than lookback+1
status, rvol, detail = compute_rvol(short_df, cfg)
check("Insufficient candles -> rvol is None, status still OK (not a crash)", status == OK and rvol is None)

# -- Exact RVOL calculation, hand-verified ----------------------------------

# 20 baseline candles at volume=1000 each (avg=1000), then current candle at volume=2000
volumes = [1000] * 20 + [2000]
df = make_df(volumes)
status, rvol, detail = compute_rvol(df, cfg)
check("RVOL calculation: current=2000, avg=1000 -> rvol == 2.0 exactly", status == OK and rvol == 2.0)
check("RVOL detail includes current_volume/avg_volume/label", all(k in detail for k in ("current_volume", "avg_volume", "label")))
check("RVOL=2.0 correctly labeled 'very strong'", detail["label"] == "very strong")

# -- Current candle's own volume must NOT be included in its own baseline --

volumes2 = [1000] * 20 + [100000]  # huge current volume
df2 = make_df(volumes2)
status, rvol2, detail2 = compute_rvol(df2, cfg)
check("Current candle's huge volume does not inflate its own baseline average",
      detail2["avg_volume"] == 1000.0)
check("RVOL correctly reflects current vs UNINFLATED baseline (100)", rvol2 == 100.0)

# -- Threshold gating ---------------------------------------------------------

weak_volumes = [1000] * 20 + [800]  # rvol = 0.8, below threshold
weak_df = make_df(weak_volumes)
passes, rvol3, detail3 = passes_rvol_threshold(weak_df, cfg)
check("RVOL below threshold (0.8 < 1.5) -> passes=False", passes is False)

strong_volumes = [1000] * 20 + [1600]  # rvol = 1.6, above threshold
strong_df = make_df(strong_volumes)
passes4, rvol4, detail4 = passes_rvol_threshold(strong_df, cfg)
check("RVOL above threshold (1.6 >= 1.5) -> passes=True", passes4 is True)

# -- Fail-closed when RVOL can't be computed --------------------------------

passes5, rvol5, detail5 = passes_rvol_threshold(short_df, cfg)
check("Insufficient data -> passes_rvol_threshold fails CLOSED (False), not open", passes5 is False)

# -- Never raises on malformed input -----------------------------------------

malformed_df = pd.DataFrame({"close": [1, 2, 3]})  # no 'volume' column
try:
    status, rvol, detail = compute_rvol(malformed_df, cfg)
    check("Malformed DataFrame (missing volume column) -> handled gracefully, no crash", rvol is None)
except Exception as e:
    check(f"Should never raise, but got: {e}", False)

# -- Logging never crashes ----------------------------------------------------

line = format_rvol_log("TEST", 1.8, {"threshold": 1.5, "passes": True, "label": "strong"})
check("format_rvol_log works with a real value", isinstance(line, str) and "1.8" in line)
line2 = format_rvol_log("TEST", None, {"reason": "insufficient data"})
check("format_rvol_log works with rvol_value=None", isinstance(line2, str) and "N/A" in line2)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
