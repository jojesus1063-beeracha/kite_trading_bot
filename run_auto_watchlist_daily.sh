#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/home/ubuntu/kite_trading_bot"
PYTHON="$PROJECT/venv/bin/python3"
LOCK_FILE="/run/kite-auto-watchlist.lock"

cd "$PROJECT"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "Another automatic-watchlist run is already active."
    exit 1
fi

echo "Automatic watchlist started at:"
TZ=Asia/Kolkata date

"$PYTHON" - <<'PY'
import json
from pathlib import Path

config = json.loads(
    Path("user_config.json").read_text()
)

if config.get("paper_trading") is not True:
    raise SystemExit(
        "SAFETY BLOCK: automatic startup is authorised "
        "only while Paper Trading is enabled."
    )

print("PASS: paper-trading safety lock enabled")
PY

"$PYTHON" auto_watchlist.py \
  --write \
  --top 80 \
  --min-selected 80 \
  --open-low-tolerance-ticks 0 \
  --min-live-momentum-pct 0.20

"$PYTHON" - <<'PY'
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ist = ZoneInfo("Asia/Kolkata")

config = json.loads(
    Path("user_config.json").read_text()
)

report = json.loads(
    Path(
        "runtime/auto_watchlist/latest_report.json"
    ).read_text()
)

if report.get("status") != "success":
    raise SystemExit(
        "FAIL: selector report is not successful"
    )

generated = datetime.fromisoformat(
    report["generated_at"]
).astimezone(ist)

if generated.date() != datetime.now(ist).date():
    raise SystemExit(
        "FAIL: selector report was not generated today"
    )

watchlist = config.get("watchlist") or []

if len(watchlist) != 80:
    raise SystemExit(
        f"FAIL: expected 80 stocks, found {len(watchlist)}"
    )

symbols = [
    item.get("symbol")
    for item in watchlist
]

if len(symbols) != len(set(symbols)):
    raise SystemExit(
        "FAIL: duplicate watchlist symbols detected"
    )

if config.get("paper_trading") is not True:
    raise SystemExit(
        "FAIL: Paper Trading became disabled"
    )

stats = report.get("statistics") or {}
counts = stats.get("selected_priority_counts") or {}

if sum(counts.values()) != 80:
    raise SystemExit(
        "FAIL: priority counts do not total 80"
    )

print("PASS: today's 80-stock watchlist validated")
print("Priority counts:", counts)
print("Symbols:", ", ".join(symbols))
PY

echo "Automatic watchlist completed at:"
TZ=Asia/Kolkata date
