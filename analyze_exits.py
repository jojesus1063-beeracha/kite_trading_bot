#!/usr/bin/env python3
import re
import sys
from collections import defaultdict
from pathlib import Path

LOG_FILE = Path("fno_bot.log")

ENTRY_PATTERN = re.compile(
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+).*?ENTRY FILLED.*?price=(?P<price>[\d.]+)"
)
EXIT_PATTERN = re.compile(
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+).*?EXIT FILLED.*?reason=(?P<reason>\w+).*?price=(?P<price>[\d.]+).*?pnl=(?P<pnl>[-\d.]+)"
)


def parse_logs(file_path: Path):
    if not file_path.exists():
        print(f"Error: Log file '{file_path}' not found.")
        sys.exit(1)

    trades = []
    current_trade = None

    with open(file_path, "r") as f:
        for line in f:
            entry_match = ENTRY_PATTERN.search(line)
            if entry_match:
                current_trade = {
                    "entry_time": entry_match.group("time"),
                    "entry_price": float(entry_match.group("price")),
                }
                continue

            exit_match = EXIT_PATTERN.search(line)
            if exit_match and current_trade:
                current_trade["exit_time"] = exit_match.group("time")
                current_trade["exit_reason"] = exit_match.group("reason")
                current_trade["exit_price"] = float(exit_match.group("price"))
                current_trade["pnl"] = float(exit_match.group("pnl"))
                trades.append(current_trade)
                current_trade = None

    return trades


def analyze(trades):
    if not trades:
        print("No completed trades found in log file.")
        return

    stats = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "wins": 0})

    for t in trades:
        reason = t["exit_reason"]
        stats[reason]["count"] += 1
        stats[reason]["total_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            stats[reason]["wins"] += 1

    total_trades = len(trades)
    print(f"\n================ TRADE EXIT ANALYSIS ================")
    print(f"Total Trades Evaluated: {total_trades}\n")
    print(f"{'Exit Reason':<20} | {'Count':<7} | {'% Total':<8} | {'Win Rate':<10} | {'Total PnL':<10}")
    print("-" * 65)

    for reason, data in stats.items():
        count = data["count"]
        pct_total = (count / total_trades) * 100
        win_rate = (data["wins"] / count) * 100
        total_pnl = data["total_pnl"]
        print(
            f"{reason:<20} | {count:<7} | {pct_total:>6.1f}% | {win_rate:>8.1f}% | ₹{total_pnl:>9.2f}"
        )

    print("=" * 65)


if __name__ == "__main__":
    target_log = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_FILE
    trades = parse_logs(target_log)
    analyze(trades)
