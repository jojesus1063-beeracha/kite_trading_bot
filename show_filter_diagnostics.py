"""Print the latest paper-trading filter rejection snapshot.

Usage:
    python show_filter_diagnostics.py

This reads diagnostics only. It never imports or starts the trading loop.
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / "runtime" / "filter_diagnostics" / "latest.json"

LABELS = {
    "TREND_OR_ADX": "15m trend / ADX",
    "ENTRY_DATA": "Entry indicator data",
    "ENTRY_EMA_OR_VOLUME": "5m entry EMA / base volume",
    "VWAP_ACCEPTANCE": "VWAP acceptance",
    "EMA200_CONFIRMATION": "Full EMA200 confirmation",
    "EMA200_DIRECTIONAL": "EMA200 directional gate",
    "RVOL": "RVOL confirmation",
    "INVALID_RISK_GEOMETRY": "Invalid stop/risk geometry",
    "STRATEGY_SIGNAL": "Strategy signal before post-gates",
    "FILTERS_PASSED": "Passed EMA200 + VWAP + RVOL gates",
}


def main() -> None:
    if not PATH.exists():
        print("No filter diagnostics snapshot yet.")
        print("Run the paper bot through at least one completed entry scan.")
        return

    data = json.loads(PATH.read_text(encoding="utf-8"))
    summary = data.get("summary", {})

    print("FILTER DIAGNOSTICS")
    print("==================")
    print("scan_key:", data.get("scan_key"))
    print("updated_at:", data.get("updated_at"))
    print("symbols attributed:", data.get("symbol_count", 0))
    print()

    if not summary:
        print("No attributed symbols yet.")
        return

    for status, count in sorted(summary.items(), key=lambda item: (-item[1], item[0])):
        print(f"{count:3}  {LABELS.get(status, status)} [{status}]")

    print("\nPer-symbol details:")
    for symbol, item in sorted(data.get("symbols", {}).items()):
        status = item.get("status", "UNKNOWN")
        print(f"{symbol:<14} {LABELS.get(status, status)}")


if __name__ == "__main__":
    main()
