#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/home/ubuntu/kite_trading_bot"
PYTHON="$PROJECT/venv/bin/python3"
RUNTIME="$PROJECT/runtime/live_watchlist"
LOCK_FILE="$RUNTIME/live_daily.lock"
EXPECTED_ACK="I_ACCEPT_REAL_ORDERS"

cd "$PROJECT"
mkdir -p "$RUNTIME"
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "Another LIVE automatic-watchlist run is active."
    exit 1
fi

if [[ "${KITE_LIVE_COMBINED_ACK:-}" != "$EXPECTED_ACK" ]]; then
    echo "SAFETY BLOCK: KITE_LIVE_COMBINED_ACK acknowledgement is missing."
    exit 1
fi

"$PYTHON" - <<'PY'
import json
from pathlib import Path

path = Path("user_config.json")
if not path.exists():
    raise SystemExit("SAFETY BLOCK: user_config.json not found")
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("paper_trading") is not False:
    raise SystemExit("SAFETY BLOCK: live selector requires paper_trading=false")
print("PASS: LIVE mode confirmed")
PY

# Timer fires at 09:26:50. Begin the first quote request at approximately
# 09:27:10, matching the validated frozen top-60 selection boundary.
echo "Waiting 20 seconds for the 09:27:10 selector boundary..."
sleep 20

echo "Read-only full-universe LIVE top-60 selection started:"
TZ=Asia/Kolkata date

"$PYTHON" paper_full_universe_top60_selector.py \
  --top 60 \
  --min-selected 60 \
  --max-price 2200 \
  --min-turnover 1000000 \
  --max-spread-pct 0.25 \
  --min-circuit-distance-pct 1.0 \
  --min-abs-change-pct 0.30 \
  --min-day-range-pct 0.40 \
  --history-candidates 120 \
  --output runtime/live_watchlist/latest_watchlist.json \
  --report runtime/live_watchlist/latest_report.json

# This step is the only config write. It first verifies today's selector
# contract, local journals, broker MIS flatness, and the exact live acknowledgement.
"$PYTHON" live_combined_preflight.py \
  --check-broker-flat \
  --apply-watchlist

echo "Combined LIVE top-60 handoff completed:"
TZ=Asia/Kolkata date
