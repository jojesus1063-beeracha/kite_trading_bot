#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/home/ubuntu/kite_trading_bot"
PYTHON="$PROJECT/venv/bin/python3"
LOCK_DIR="$PROJECT/runtime/auto_watchlist"
LOCK_FILE="$LOCK_DIR/paper_daily.lock"

cd "$PROJECT"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "Another PAPER automatic-watchlist run is active."
    exit 1
fi

"$PYTHON" - <<'PY'
import json
from pathlib import Path

path = Path("user_config.json")
if not path.exists():
    raise SystemExit("SAFETY BLOCK: user_config.json not found")

data = json.loads(path.read_text())
if data.get("paper_trading") is not True:
    raise SystemExit(
        "SAFETY BLOCK: NSE top-movers selector requires paper_trading=true"
    )
print("PASS: PAPER mode confirmed")
PY

# The systemd unit fires at 09:15:50 and stops the PAPER bot first. Waiting
# twenty seconds makes the first quote request begin at approximately 09:16:10,
# after the opening minute has started forming, while preventing the old
# watchlist from trading during selection.
echo "Waiting 20 seconds for the 09:16:10 selector boundary..."
sleep 20

echo "NSE top-10 gainers + top-10 losers PAPER generation started:"
TZ=Asia/Kolkata date

"$PYTHON" paper_nse_top_movers_selector.py \
  --write \
  --winners 10 \
  --losers 10 \
  --min-price 20 \
  --max-price 2200 \
  --min-turnover 1000000 \
  --max-spread-pct 0.25 \
  --min-circuit-distance-pct 1.0 \
  --output runtime/auto_watchlist/latest_watchlist.json \
  --report runtime/auto_watchlist/latest_report.json

"$PYTHON" - <<'PY'
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ist = ZoneInfo("Asia/Kolkata")
config = json.loads(Path("user_config.json").read_text())
report = json.loads(Path("runtime/auto_watchlist/latest_report.json").read_text())
payload = json.loads(Path("runtime/auto_watchlist/latest_watchlist.json").read_text())

if config.get("paper_trading") is not True:
    raise SystemExit("FAIL: configuration left PAPER mode")
if report.get("status") != "success" or report.get("paper_only") is not True:
    raise SystemExit("FAIL: selector report is not successful/PAPER-only")
if report.get("strategy") != "NSE_TOP10_GAINERS_TOP10_LOSERS":
    raise SystemExit("FAIL: unexpected selector strategy")
if report.get("mode") != "WRITE_CONFIG":
    raise SystemExit("FAIL: selector did not run in WRITE_CONFIG mode")

generated = datetime.fromisoformat(report["generated_at"]).astimezone(ist)
if generated.date() != datetime.now(ist).date():
    raise SystemExit("FAIL: selector report was not generated today")

watchlist = config.get("watchlist") or []
output_watchlist = payload.get("watchlist") or []
selected = report.get("selected") or []
if len(watchlist) != 20 or len(output_watchlist) != 20 or len(selected) != 20:
    raise SystemExit(
        f"FAIL: expected exactly 20 stocks; config={len(watchlist)} "
        f"output={len(output_watchlist)} report={len(selected)}"
    )

symbols = [str(item.get("symbol") or "").strip() for item in watchlist]
if any(not symbol for symbol in symbols):
    raise SystemExit("FAIL: blank symbol found")
if len(symbols) != len(set(symbols)):
    raise SystemExit("FAIL: duplicate symbol found in NSE watchlist")
if any(item.get("exchange") != "NSE" for item in watchlist):
    raise SystemExit("FAIL: non-NSE row found")
if any(item.get("ordinary_equity_clean") is not True for item in selected):
    raise SystemExit("FAIL: selected row missing ordinary-equity-clean marker")

cleaning = report.get("cleaning") or {}
clean_total = int(cleaning.get("clean_nse_equities") or 0)
quotes_received = int(report.get("quotes_received") or 0)
if clean_total < 20:
    raise SystemExit(f"FAIL: cleaned universe too small: {clean_total}")
minimum_quotes = math.ceil(clean_total * 0.90)
if quotes_received < minimum_quotes:
    raise SystemExit(
        f"FAIL: quote coverage too low: {quotes_received}/{clean_total}; "
        f"minimum={minimum_quotes}"
    )

gainers = report.get("selected_gainers") or []
losers = report.get("selected_losers") or []
if len(gainers) != 10 or len(losers) != 10:
    raise SystemExit(
        f"FAIL: expected 10 gainers and 10 losers; got {len(gainers)} and {len(losers)}"
    )
if any(float(item.get("change_pct") or 0) <= 0 for item in gainers):
    raise SystemExit("FAIL: non-positive row found in gainers")
if any(float(item.get("change_pct") or 0) >= 0 for item in losers):
    raise SystemExit("FAIL: non-negative row found in losers")

if watchlist != output_watchlist:
    raise SystemExit("FAIL: user_config and selector output watchlists differ")

print("PASS: NSE top-movers PAPER watchlist validated")
print("Raw NSE instruments:", cleaning.get("raw_nse_instruments"))
print("Clean ordinary equities:", clean_total)
print("Cleaning rejections:", cleaning.get("cleaning_rejections"))
print("Eligible after safety filters:", report.get("eligible_after_safety_filters"))
print("Gainers:", [(x.get("symbol"), x.get("change_pct")) for x in gainers])
print("Losers:", [(x.get("symbol"), x.get("change_pct")) for x in losers])
print("Watchlist count:", len(watchlist))
PY

echo "NSE top-10 gainers + top-10 losers PAPER generation completed:"
TZ=Asia/Kolkata date
