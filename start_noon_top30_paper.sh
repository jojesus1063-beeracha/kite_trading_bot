#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ubuntu/kite_trading_bot
source venv/bin/activate

TS=$(date +%Y%m%d_%H%M%S)
BACKUP="runtime/session_backups/${TS}_noon_top30"
mkdir -p "$BACKUP"

echo "======================================================"
echo "STARTING FRESH NOON TOP-30 PAPER SESSION"
date
echo "======================================================"

# ------------------------------------------------------
# 1. STOP OLD PAPER PROCESSES
# ------------------------------------------------------
sudo systemctl stop kitebot-paper-contrarian.service || true
sudo systemctl stop kitebot-equity-socket-shadow.service || true

# ------------------------------------------------------
# 2. BACK UP EVERYTHING WE ARE ABOUT TO RESET/REPLACE
# ------------------------------------------------------
cp day_state.json \
   "$BACKUP/day_state.before_noon.json" \
   2>/dev/null || true

cp open_positions.json \
   "$BACKUP/open_positions.before_noon.json" \
   2>/dev/null || true

cp user_config.json \
   "$BACKUP/user_config.before_noon.json"

cp runtime/auto_watchlist/latest_watchlist.json \
   "$BACKUP/watchlist.before_noon.json" \
   2>/dev/null || true

cp runtime/auto_watchlist/latest_report.json \
   "$BACKUP/watchlist_report.before_noon.json" \
   2>/dev/null || true

cp paper_contrarian_launcher.py \
   "$BACKUP/paper_contrarian_launcher.py"

echo "BACKUP=$BACKUP"

# ------------------------------------------------------
# 3. HARD-ENABLE ONE SYMBOL / ONE TRADE PER CALENDAR DAY
#
# Existing history is NOT deleted.
# Morning trades therefore remain visible to this guard.
# ------------------------------------------------------
python3 - <<'PY'
from pathlib import Path

p = Path("paper_contrarian_launcher.py")
s = p.read_text()

old = '''                max_per_symbol=1,
                force_active=live_combined,
                fail_closed=live_combined,
'''

new = '''                max_per_symbol=1,
                force_active=(
                    live_combined
                    or bool(getattr(cfg, "PAPER_TRADING", False))
                ),
                fail_closed=(
                    live_combined
                    or bool(getattr(cfg, "PAPER_TRADING", False))
                ),
'''

if new in s:
    print("SYMBOL_DAILY_GUARD=ALREADY_PATCHED")
elif old in s:
    s = s.replace(old, new, 1)
    p.write_text(s)
    print("SYMBOL_DAILY_GUARD=PATCHED")
else:
    raise SystemExit(
        "ABORT: expected paper entry-guard call not found"
    )
PY

python3 -m py_compile \
    paper_contrarian_launcher.py \
    paper_depth_launcher.py \
    main.py \
    strategy.py

echo "PY_COMPILE=PASS"

# ------------------------------------------------------
# 4. GENERATE FRESH TOP-30 MOMENTUM WATCHLIST
#
# We intentionally bypass run_paper_watchlist_daily.sh
# because its strategy-name validation was originally
# written around TOP120.
#
# Keep history-candidates=120:
# evaluate a broader pool, retain strongest final 30.
# ------------------------------------------------------
echo
echo "===== BUILD FRESH TOP-30 ====="

python3 paper_full_universe_top60_selector.py \
  --write \
  --top 30 \
  --min-selected 30 \
  --max-price 2200 \
  --min-turnover 1000000 \
  --max-spread-pct 0.25 \
  --min-circuit-distance-pct 1.0 \
  --min-abs-change-pct 0.30 \
  --min-day-range-pct 0.40 \
  --history-candidates 120 \
  --output runtime/auto_watchlist/latest_watchlist.json \
  --report runtime/auto_watchlist/latest_report.json

# ------------------------------------------------------
# 5. STRICTLY VALIDATE THAT WE REALLY GOT 30
# ------------------------------------------------------
python3 - <<'PY'
import json
from pathlib import Path

cfg = json.loads(Path("user_config.json").read_text())
report = json.loads(
    Path("runtime/auto_watchlist/latest_report.json").read_text()
)
payload = json.loads(
    Path("runtime/auto_watchlist/latest_watchlist.json").read_text()
)

assert cfg.get("paper_trading") is True, \
    "FAIL: not in PAPER mode"

config_wl = cfg.get("watchlist") or []
output_wl = payload.get("watchlist") or []
selected = report.get("selected") or []

assert len(config_wl) == 30, \
    f"FAIL: config watchlist={len(config_wl)}"

assert len(output_wl) == 30, \
    f"FAIL: output watchlist={len(output_wl)}"

assert len(selected) == 30, \
    f"FAIL: report selected={len(selected)}"

symbols = [
    str(x.get("symbol") or "").strip()
    for x in config_wl
]

assert len(set(symbols)) == 30, \
    "FAIL: duplicate symbols in Top-30"

assert config_wl == output_wl, \
    "FAIL: config/output watchlists differ"

print("TOP30_VALIDATION=PASS")
print("SELECTED_COUNT=30")
print("HISTORY_POOL=", report.get("history_pool_size"))
print()
print("TOP-30 SYMBOLS:")
for i, row in enumerate(selected, 1):
    print(
        f"{i:02d}. "
        f"{row.get('symbol'):15s} "
        f"{row.get('exchange'):3s} "
        f"score={row.get('final_score', row.get('score'))}"
    )
PY

# ------------------------------------------------------
# 6. SAVE TODAY'S ALREADY-TRADED SYMBOLS FOR AUDIT
#    Do NOT remove trade_history.jsonl.
# ------------------------------------------------------
python3 - <<'PY'
import json
from pathlib import Path
from datetime import date

today = date.today().isoformat()
symbols = set()

p = Path("trade_history.jsonl")

if p.exists():
    for line in p.open(errors="replace"):
        try:
            x = json.loads(line)
        except Exception:
            continue

        text = json.dumps(x, default=str)

        if today not in text:
            continue

        symbol = str(x.get("symbol") or "").strip().upper()

        if symbol:
            symbols.add(symbol)

out = Path(
    "runtime/noon_session_used_symbols.json"
)

out.write_text(
    json.dumps(
        {
            "date": today,
            "rule": "ONE_SYMBOL_ONE_TRADE_PER_CALENDAR_DAY",
            "symbols_already_used": sorted(symbols),
        },
        indent=2,
    ) + "\n"
)

print(
    "SYMBOLS_ALREADY_USED_TODAY=",
    len(symbols)
)

print(
    ", ".join(sorted(symbols))
    if symbols
    else "NONE"
)
PY

# ------------------------------------------------------
# 7. CLEAR OLD PERSISTED PAPER POSITIONS
#
# Snapshot already backed up above.
# Fresh noon session begins with zero positions.
# ------------------------------------------------------
python3 - <<'PY'
from position_store import clear_positions

clear_positions()

print("PERSISTED_POSITIONS=CLEARED")
PY

# ------------------------------------------------------
# 8. RESET ONLY THE NEW SESSION'S GLOBAL RISK COUNTER
#
# Trade history remains intact specifically so the
# per-symbol daily guard still knows morning symbols.
# ------------------------------------------------------
python3 - <<'PY'
import json
from datetime import date
from pathlib import Path

state = {
    "date": date.today().isoformat(),
    "trades_taken": 0,
    "realized_pnl": 0.0,
    "consecutive_losses": 0,
    "halted": False,
    "halt_reason": "",
}

Path("day_state.json").write_text(
    json.dumps(state, indent=2) + "\n"
)

print("DAY_STATE_RESET=PASS")
print(json.dumps(state, indent=2))
PY

# ------------------------------------------------------
# 9. VERIFY PAPER STRATEGY SETTINGS
# ------------------------------------------------------
python3 - <<'PY'
import paper_depth_launcher as p

assert p.PAPER_CAPITAL == 5000.0
assert p.PAPER_MAX_OPEN_POSITIONS == 3
assert p.PAPER_MAX_TRADES_PER_DAY == 7
assert abs(
    p.PAPER_MAX_EMA_DISTANCE_ATR - 0.25
) < 1e-12

print("PAPER_CAPITAL=5000")
print("MAX_OPEN_POSITIONS=3")
print("MAX_TRADES_PER_SESSION=7")
print("FINAL_EMA9_DISTANCE_MAX=0.25 ATR")
print("PAPER_SETTINGS=PASS")
PY

# ------------------------------------------------------
# 10. START NEW TOP-30 STACK
# ------------------------------------------------------
echo
echo "===== START SOCKET RECORDER ====="

sudo systemctl start \
    kitebot-equity-socket-shadow.service

sleep 3

echo
echo "===== START PAPER BOT ====="

sudo systemctl start \
    kitebot-paper-contrarian.service

sleep 10

# ------------------------------------------------------
# 11. FINAL VERIFICATION
# ------------------------------------------------------
echo
echo "===== SERVICE STATUS ====="

echo -n "PAPER_BOT="
systemctl is-active \
    kitebot-paper-contrarian.service

echo -n "DEPTH_SOCKET="
systemctl is-active \
    kitebot-equity-socket-shadow.service

echo
echo "===== RISK STATE ====="
cat day_state.json

echo
echo "===== STARTUP HEALTH ====="

journalctl \
  -u kitebot-paper-contrarian.service \
  --since "2 minutes ago" \
  --no-pager -l | \
grep -E \
'PAPER CLEAN PIPELINE|PAPER DEPTH MODE|STARTUP HEALTH|Starting PAPER|ERROR|CRITICAL' \
| tail -40 || true

echo
echo "===== WATCHLIST SIZE FROM STARTUP ====="

journalctl \
  -u kitebot-paper-contrarian.service \
  --since "2 minutes ago" \
  --no-pager -l | \
grep -E \
'watchlist_size=|Starting PAPER trading on' \
| tail -10 || true

echo
echo "===== FINAL EMA GATE ====="

journalctl \
  -u kitebot-paper-contrarian.service \
  --since "2 minutes ago" \
  --no-pager -l | \
grep -E \
'FINAL EMA DISTANCE CHECK|FINAL_EMA_DISTANCE_REJECTED' \
| tail -20 || true

echo
echo "======================================================"
echo "FRESH TOP-30 PAPER SESSION ACTIVE"
echo "ONE SYMBOL = MAX ONE TRADE FOR ENTIRE CALENDAR DAY"
echo "SESSION TRADE COUNTER = 0 / 7"
echo "======================================================"
