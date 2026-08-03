#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/home/ubuntu/kite_trading_bot"
PYTHON="$PROJECT/venv/bin/python3"
LOCK_DIR="$PROJECT/runtime/auto_watchlist"
LOCK_FILE="$LOCK_DIR/daily.lock"

cd "$PROJECT"
mkdir -p "$LOCK_DIR"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "Another automatic-watchlist run is active."
    exit 1
fi

if [[ "${ALLOW_LIVE_AUTOSTART:-}" != "YES" ]]; then
    echo "SAFETY BLOCK: live automatic startup is not authorised."
    exit 1
fi

echo "Live watchlist generation started:"
TZ=Asia/Kolkata date

"$PYTHON" - <<'PY'
import json
import config as cfg
from pathlib import Path

data = json.loads(Path("user_config.json").read_text())

if data.get("paper_trading") is not False:
    raise SystemExit(
        "SAFETY BLOCK: configuration is not in live mode."
    )

checks = {
    "capital": (
        float(data.get("capital", 0)),
        5000.0,
    ),
    "risk_per_trade_pct": (
        float(data.get("risk_per_trade_pct", 999)),
        0.5,
    ),
    "max_daily_loss_pct": (
        float(data.get("max_daily_loss_pct", 999)),
        2.0,
    ),
    "max_position_size_pct": (
        float(data.get("max_position_size_pct", 999)),
        50.0,
    ),
}

for name, (actual, maximum) in checks.items():
    if actual > maximum:
        raise SystemExit(
            f"SAFETY BLOCK: {name} is {actual}; "
            f"maximum authorised is {maximum}."
        )

if int(getattr(cfg, "MAX_OPEN_POSITIONS", 999)) > 5:
    raise SystemExit(
        "SAFETY BLOCK: MAX_OPEN_POSITIONS exceeds 5."
    )

if data.get("no_entry_before") != "09:30":
    raise SystemExit(
        "SAFETY BLOCK: no_entry_before must remain 09:30."
    )

print("PASS: live-mode safety limits validated")
print("Capital:", data.get("capital"))
print("Risk per trade:", data.get("risk_per_trade_pct"))
print("Daily-loss limit:", data.get("max_daily_loss_pct"))
print("No entry before:", data.get("no_entry_before"))
PY

"$PYTHON" auto_watchlist.py \
  --write \
  --top 80 \
  --min-selected 1 \
  --open-low-tolerance-ticks 0 \
  --min-live-momentum-pct 0.20

"$PYTHON" - <<'PY'
import json
import math
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
        "FAIL: automatic selector did not succeed."
    )

generated = datetime.fromisoformat(
    report["generated_at"]
).astimezone(ist)

if generated.date() != datetime.now(ist).date():
    raise SystemExit(
        "FAIL: selector report was not generated today."
    )

watchlist = config.get("watchlist") or []

stats = report.get("statistics") or {}

matched_symbols = int(
    stats.get("matched_symbols") or 0
)
quotes_received = int(
    stats.get("quotes_received") or 0
)

minimum_quotes = math.ceil(
    matched_symbols * 0.90
)

if matched_symbols < 450:
    raise SystemExit(
        "FAIL: too few NSE symbols matched: "
        f"{matched_symbols}. Minimum required is 450."
    )

if quotes_received < minimum_quotes:
    raise SystemExit(
        "FAIL: incomplete Kite quote coverage: "
        f"{quotes_received}/{matched_symbols}. "
        "At least 90% is required."
    )

if not 1 <= len(watchlist) <= 80:
    raise SystemExit(
        "FAIL: expected between 1 and 80 qualified "
        f"stocks; found {len(watchlist)}."
    )

symbols = [
    str(item.get("symbol") or "").strip()
    for item in watchlist
]

if any(not symbol for symbol in symbols):
    raise SystemExit(
        "FAIL: blank symbol found in watchlist."
    )

if len(symbols) != len(set(symbols)):
    raise SystemExit(
        "FAIL: duplicate watchlist symbols found."
    )

if config.get("paper_trading") is not False:
    raise SystemExit(
        "FAIL: configuration is no longer in live mode."
    )

stats = report.get("statistics") or {}
priority_counts = (
    stats.get("selected_priority_counts") or {}
)

if sum(priority_counts.values()) != len(watchlist):
    raise SystemExit(
        "FAIL: priority counts do not match the "
        "generated watchlist size."
    )

print("PASS: live watchlist validated")
print("Priority counts:", priority_counts)
print("Watchlist count:", len(watchlist))
PY

echo "Live watchlist generation completed:"
TZ=Asia/Kolkata date
