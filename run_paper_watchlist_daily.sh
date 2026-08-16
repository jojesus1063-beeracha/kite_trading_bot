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
        "SAFETY BLOCK: cleaned full-universe selector requires paper_trading=true"
    )
print("PASS: PAPER mode confirmed")
PY

# The systemd unit fires at 09:26:50 and stops the PAPER bot first. Waiting
# twenty seconds makes the first quote request begin at approximately 09:27:10,
# after four completed 3-minute candles establish the frozen selection boundary, while preventing the old
# watchlist from trading during selection.
echo "Waiting 20 seconds for the 09:27:10 selector boundary..."
sleep 20

echo "Clean full-universe PAPER top-60 generation started:"
TZ=Asia/Kolkata date

"$PYTHON" paper_full_universe_top60_selector.py \
  --write \
  --top 60 \
  --min-selected 60 \
  --max-price 2200 \
  --min-turnover 1000000 \
  --max-spread-pct 0.25 \
  --min-circuit-distance-pct 1.0 \
  --min-abs-change-pct 0.30 \
  --min-day-range-pct 0.40 \
  --history-candidates 120 \
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
if report.get("strategy") != "FULL_ZERODHA_CLEAN_TOP60_MOMENTUM":
    raise SystemExit("FAIL: unexpected selector strategy")
if report.get("mode") != "WRITE_CONFIG":
    raise SystemExit("FAIL: selector did not run in WRITE_CONFIG mode")

generated = datetime.fromisoformat(report["generated_at"]).astimezone(ist)
if generated.date() != datetime.now(ist).date():
    raise SystemExit("FAIL: selector report was not generated today")

watchlist = config.get("watchlist") or []
output_watchlist = payload.get("watchlist") or []
selected = report.get("selected") or []
if len(watchlist) != 60 or len(output_watchlist) != 60 or len(selected) != 60:
    raise SystemExit(
        f"FAIL: expected exactly 60 stocks; config={len(watchlist)} "
        f"output={len(output_watchlist)} report={len(selected)}"
    )

symbols = [str(item.get("symbol") or "").strip() for item in watchlist]
if any(not symbol for symbol in symbols):
    raise SystemExit("FAIL: blank symbol found")
if len(symbols) != len(set(symbols)):
    raise SystemExit("FAIL: duplicate symbol found across NSE/BSE")
if any(item.get("exchange") not in {"NSE", "BSE"} for item in watchlist):
    raise SystemExit("FAIL: non-NSE/BSE row found")
if any(item.get("ordinary_equity_clean") is not True for item in selected):
    raise SystemExit("FAIL: selected row missing ordinary-equity-clean marker")

cleaning = report.get("cleaning") or {}
clean_total = int(cleaning.get("clean_total") or 0)
quotes_received = int(report.get("quotes_received") or 0)
if clean_total < 60:
    raise SystemExit(f"FAIL: cleaned universe too small: {clean_total}")
minimum_quotes = math.ceil(clean_total * 0.90)
if quotes_received < minimum_quotes:
    raise SystemExit(
        f"FAIL: quote coverage too low: {quotes_received}/{clean_total}; "
        f"minimum={minimum_quotes}"
    )

hard = report.get("hard_filter") or {}
expected_hard = {
    "min_price": 20.0,
    "max_price": 2200.0,
    "min_turnover": 1000000.0,
    "max_spread_pct": 0.25,
    "min_circuit_distance_pct": 1.0,
    "min_abs_change_pct": 0.30,
    "min_day_range_pct": 0.40,
}
for name, expected in expected_hard.items():
    actual = float(hard.get(name, float("nan")))
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
        raise SystemExit(f"FAIL: {name} changed: {actual} != {expected}")

expected_weights = {
    "turnover": 30,
    "movement": 25,
    "volume": 20,
    "spread": 10,
    "depth": 5,
    "directional_open_extreme": 5,
    "previous_day_momentum": 5,
}
if report.get("score_weights") != expected_weights:
    raise SystemExit(
        f"FAIL: scoring weights changed: {report.get('score_weights')}"
    )

if watchlist != output_watchlist:
    raise SystemExit("FAIL: user_config and selector output watchlists differ")

print("PASS: cleaned full-universe PAPER watchlist validated")
print("Raw instruments:", cleaning.get("raw_total"))
print("Clean ordinary equities:", clean_total)
print("Cleaning rejections:", cleaning.get("cleaning_rejections"))
print("Eligible after dedupe:", report.get("eligible_after_symbol_dedupe"))
print("Selected exchanges:", report.get("selected_exchange_counts"))
print("Open-extreme counts:", report.get("selected_open_extreme_counts"))
print("Watchlist count:", len(watchlist))
PY

echo "Clean full-universe PAPER top-60 generation completed:"
TZ=Asia/Kolkata date
