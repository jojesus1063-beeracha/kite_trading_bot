#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/home/ubuntu/kite_trading_bot"
PYTHON="$PROJECT/venv/bin/python3"
LOCK_DIR="$PROJECT/runtime/paper_watchlist"
LOCK_FILE="$LOCK_DIR/daily.lock"

cd "$PROJECT"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "Another paper-watchlist run is active."
    exit 1
fi

"$PYTHON" - <<'PY'
import json
from pathlib import Path

config_path = Path("user_config.json")
if not config_path.exists():
    raise SystemExit("SAFETY BLOCK: user_config.json not found")

data = json.loads(config_path.read_text())
if data.get("paper_trading") is not True:
    raise SystemExit(
        "SAFETY BLOCK: paper famine selector requires paper_trading=true"
    )
print("PASS: paper mode confirmed")
PY

echo "Paper momentum + famine watchlist generation started:"
TZ=Asia/Kolkata date

"$PYTHON" paper_watchlist_selector.py \
  --write \
  --momentum-min-pct 0.75 \
  --famine-rvol-min 0.40 \
  --famine-rvol-max 0.70 \
  --baseline-days 20 \
  --earliest-famine-time 09:45 \
  --top 60

"$PYTHON" - <<'PY'
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ist = ZoneInfo("Asia/Kolkata")
config = json.loads(Path("user_config.json").read_text())
report = json.loads(Path("runtime/paper_watchlist/latest_report.json").read_text())

if config.get("paper_trading") is not True:
    raise SystemExit("FAIL: configuration left paper mode")
if report.get("status") != "success" or report.get("paper_only") is not True:
    raise SystemExit("FAIL: paper selector report is not successful/paper-only")
if report.get("strategy") != "HIGH_MOMENTUM_AND_VOLUME_FAMINE_ONLY":
    raise SystemExit("FAIL: unexpected paper selector strategy")

generated = datetime.fromisoformat(report["generated_at"]).astimezone(ist)
if generated.date() != datetime.now(ist).date():
    raise SystemExit("FAIL: paper selector report was not generated today")

watchlist = config.get("watchlist") or []
selected = report.get("selected") or []
if len(watchlist) != len(selected):
    raise SystemExit("FAIL: config/report watchlist counts differ")
if len(watchlist) > 60:
    raise SystemExit(f"FAIL: paper watchlist exceeds cap: {len(watchlist)}")

for item in selected:
    if float(item.get("momentum_pct", 0)) < 0.75:
        raise SystemExit(f"FAIL: non-momentum candidate: {item}")
    rvol = float(item.get("relative_volume", 0))
    if not 0.40 <= rvol <= 0.70:
        raise SystemExit(f"FAIL: non-famine candidate: {item}")
    if item.get("high_momentum") is not True or item.get("famine") is not True:
        raise SystemExit(f"FAIL: candidate missing required gates: {item}")

print("PASS: paper watchlist validated")
print("Qualified stocks:", len(watchlist))
PY

echo "Paper momentum + famine watchlist generation completed:"
TZ=Asia/Kolkata date
