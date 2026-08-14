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

echo "NSE breakout-ready top-25 gainers + top-15 losers PAPER generation started:"
TZ=Asia/Kolkata date

"$PYTHON" paper_nse_top_movers_selector.py \
  --write \
  --winners 25 \
  --losers 15 \
  --min-price 50 \
  --max-price 3000 \
  --min-turnover 100000000 \
  --max-spread-pct 0.15 \
  --min-circuit-distance-pct 1.0 \
  --min-abs-change-pct 1.5 \
  --max-abs-change-pct 8.0 \
  --min-day-range-pct 0.75 \
  --near-breakout-pct 0.50 \
  --min-rvol 1.20 \
  --min-atr-pct 0.30 \
  --max-atr-pct 1.50 \
  --max-vwap-distance-pct 1.00 \
  --buy-min-adx 25 \
  --sell-min-adx 20 \
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
if report.get("strategy") != "NSE_BREAKOUT_READY_TOP25_GAINERS_TOP15_LOSERS":
    raise SystemExit("FAIL: unexpected selector strategy")
if report.get("mode") != "WRITE_CONFIG":
    raise SystemExit("FAIL: selector did not run in WRITE_CONFIG mode")

generated = datetime.fromisoformat(report["generated_at"]).astimezone(ist)
if generated.date() != datetime.now(ist).date():
    raise SystemExit("FAIL: selector report was not generated today")

watchlist = config.get("watchlist") or []
output_watchlist = payload.get("watchlist") or []
selected = report.get("selected") or []
if len(watchlist) != 40 or len(output_watchlist) != 40 or len(selected) != 40:
    raise SystemExit(
        f"FAIL: expected exactly 40 stocks; config={len(watchlist)} "
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
if clean_total < 40:
    raise SystemExit(f"FAIL: cleaned universe too small: {clean_total}")
minimum_quotes = math.ceil(clean_total * 0.90)
if quotes_received < minimum_quotes:
    raise SystemExit(
        f"FAIL: quote coverage too low: {quotes_received}/{clean_total}; "
        f"minimum={minimum_quotes}"
    )

gainers = report.get("selected_gainers") or []
losers = report.get("selected_losers") or []
if len(gainers) != 25 or len(losers) != 15:
    raise SystemExit(
        f"FAIL: expected 25 gainers and 15 losers; got {len(gainers)} and {len(losers)}"
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

echo "NSE breakout-ready top-25 gainers + top-15 losers PAPER generation completed:"
TZ=Asia/Kolkata date
