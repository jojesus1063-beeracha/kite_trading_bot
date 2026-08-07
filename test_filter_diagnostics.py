from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import filter_diagnostics as fd
from rvol import format_rvol_log

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


with TemporaryDirectory() as tmpdir:
    fd._OUTPUT_DIR = Path(tmpdir)
    fd._OUTPUT_PATH = Path(tmpdir) / "latest.json"
    fd.reset_filter_diagnostics()

    scan_time = datetime(2026, 8, 7, 10, 20)

    fd.mark_filter_status("AAA", "TREND_OR_ADX", scan_time=scan_time)
    fd.mark_filter_status("BBB", "VWAP_ACCEPTANCE", scan_time=scan_time)
    summary = fd.get_filter_summary()
    check("two independent rejection reasons counted", summary == {"TREND_OR_ADX": 1, "VWAP_ACCEPTANCE": 1})
    check("snapshot file created", fd._OUTPUT_PATH.exists())

    # Deeper stages deliberately replace earlier status for the same symbol.
    fd.mark_filter_status("BBB", "STRATEGY_SIGNAL", scan_time=scan_time)
    summary = fd.get_filter_summary()
    check("later stage replaces earlier status for same symbol", summary == {"STRATEGY_SIGNAL": 1, "TREND_OR_ADX": 1})

    # RVOL formatter is observational only, but production calls it on the
    # post-strategy RVOL path. A failure should become the final attribution.
    line = format_rvol_log(
        "BBB",
        1.2,
        {"threshold": 1.5, "passes": False, "label": "average"},
    )
    summary = fd.get_filter_summary()
    check("RVOL failure becomes final attribution", summary.get("RVOL") == 1)
    check("RVOL log formatting remains intact", "value=1.20" in line)

    # A passed RVOL stage means all instrumented strategy/EMA/VWAP/RVOL gates
    # have been passed. This is diagnostic only and does not place an order.
    format_rvol_log(
        "CCC",
        1.8,
        {"threshold": 1.5, "passes": True, "label": "strong institutional participation"},
    )
    summary = fd.get_filter_summary()
    check("passed RVOL is reported as FILTERS_PASSED", summary.get("FILTERS_PASSED") == 1)

    # New 5-minute bucket must reset the previous scan rather than mix counts.
    fd.mark_filter_status(
        "DDD",
        "ENTRY_EMA_OR_VOLUME",
        scan_time=datetime(2026, 8, 7, 10, 25),
    )
    summary = fd.get_filter_summary()
    check("new scan bucket resets previous counts", summary == {"ENTRY_EMA_OR_VOLUME": 1})

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
