#!/usr/bin/env bash
# ============================================================
# PRE-LIVE OPERATIONAL AUDIT -- read-only, no changes made.
# Run this from ~/kite_trading_bot on the VM.
# Produces preflight_report.txt with evidence for every section.
# ============================================================

set -uo pipefail
cd ~/kite_trading_bot || { echo "FATAL: ~/kite_trading_bot not found"; exit 1; }

REPORT=preflight_report.txt
echo "================================================================================" > $REPORT
echo "PRE-LIVE OPERATIONAL AUDIT -- $(date)" >> $REPORT
echo "Read-only. No code, config, or git changes made by this script." >> $REPORT
echo "================================================================================" >> $REPORT

section() {
  echo "" >> $REPORT
  echo "SECTION $1: $2" >> $REPORT
  echo "--------------------------------------------------------------------------------" >> $REPORT
}

# ---------------- 1. GIT VERIFICATION ----------------
section 1 "GIT VERIFICATION"
{
  echo "-- git branch --"
  git branch
  echo ""
  echo "-- git status --"
  git status
  echo ""
  echo "-- last 5 commits --"
  git log --oneline --decorate -5
  echo ""
  echo "-- remotes --"
  git remote -v
} >> $REPORT 2>&1

# ---------------- 2. RUNTIME CONFIGURATION ----------------
section 2 "RUNTIME CONFIGURATION (effective values, after all overrides)"
python3 -c "
import config
print('PAPER_TRADING =', config.PAPER_TRADING)
print('ENABLE_WS_CANDLES =', config.ENABLE_WS_CANDLES)
print('WS_CANDLE_MODE =', config.WS_CANDLE_MODE)
print('CAPITAL =', config.CAPITAL)
print('MAX_TRADES_PER_DAY =', getattr(config, 'MAX_TRADES_PER_DAY', '<<NOT SET>>'))
print('MAX_DAILY_LOSS_PCT =', getattr(config, 'MAX_DAILY_LOSS_PCT', '<<NOT SET>>'))
print('Watchlist size =', len(config.WATCHLIST))
print('NO_ENTRY_BEFORE =', getattr(config, 'NO_ENTRY_BEFORE', '<<NOT SET>>'))
print('NO_ENTRY_AFTER =', getattr(config, 'NO_ENTRY_AFTER', '<<NOT SET>>'))
import os
print()
print('-- config precedence check --')
print('user_config.json exists:', os.path.exists('user_config.json'))
if os.path.exists('user_config.json'):
    import json
    with open('user_config.json') as f:
        uc = json.load(f)
    print('Keys present in user_config.json:', list(uc.keys()))
    print('NOTE: any key present here OVERRIDES config.py source defaults.')
    print('      Environment variables (e.g. TRADING_CAPITAL) are used only')
    print('      as the config.py-level default BEFORE user_config.json is')
    print('      applied -- user_config.json wins if the same key exists there.')
" >> $REPORT 2>&1
echo "" >> $REPORT
echo "-- TRADING_CAPITAL environment variable (only relevant if 'capital' is absent from user_config.json) --" >> $REPORT
echo "TRADING_CAPITAL=${TRADING_CAPITAL:-<<not set in this shell>>}" >> $REPORT

# ---------------- 3. WATCHLIST VERIFICATION ----------------
section 3 "WATCHLIST VERIFICATION"
python3 -c "
import config
wl = config.WATCHLIST
symbols = [w['symbol'] if isinstance(w, dict) else w for w in wl]
print('Total entries:', len(wl))
print('Unique symbols:', len(set(symbols)))
dupes = [s for s in set(symbols) if symbols.count(s) > 1]
print('Duplicates:', dupes if dupes else 'none')
empty = [w for w in wl if not (w.get('symbol') if isinstance(w, dict) else w)]
print('Empty entries:', len(empty))
exchanges = set(w.get('exchange', 'NSE') if isinstance(w, dict) else 'NSE' for w in wl)
print('Exchanges referenced:', exchanges)
print('First 5:', symbols[:5])
print('Last 5:', symbols[-5:])
" >> $REPORT 2>&1

# ---------------- 4. RUNTIME ENVIRONMENT ----------------
section 4 "RUNTIME ENVIRONMENT"
{
  echo "-- Python version --"
  python3 --version
  echo ""
  echo "-- Working directory --"
  pwd
  echo ""
  echo "-- Virtual environment --"
  echo "VIRTUAL_ENV=${VIRTUAL_ENV:-<<not active in this shell>>}"
  which python3
  echo ""
  echo "-- Key dependency versions --"
  python3 -c "import kiteconnect, pandas; print('kiteconnect:', kiteconnect.__version__ if hasattr(kiteconnect,'__version__') else 'version attr not found'); print('pandas:', pandas.__version__)" 2>&1
  echo ""
  echo "-- Write permission check for runtime state files --"
  for f in bot_status.json open_positions.json pending_orders.json trade_history.jsonl day_state.json; do
    touch "$f.write_test" 2>/dev/null && echo "$f directory: WRITABLE" && rm -f "$f.write_test" || echo "$f directory: WRITE FAILED"
  done
} >> $REPORT 2>&1

# ---------------- 5. SERVICE VERIFICATION ----------------
section 5 "SERVICE VERIFICATION"
{
  echo "-- kitebot.service --"
  systemctl status kitebot.service --no-pager 2>&1
  echo ""
  echo "-- kite-auto-watchlist.timer --"
  systemctl status kite-auto-watchlist.timer --no-pager 2>&1
  echo ""
  echo "-- any python processes currently running --"
  ps -ef | grep python | grep -v grep
  echo ""
  echo "-- is-active / is-enabled summary --"
  echo "kitebot.service is-active: $(systemctl is-active kitebot.service 2>&1)"
  echo "kitebot.service is-enabled: $(systemctl is-enabled kitebot.service 2>&1)"
  echo "kite-auto-watchlist.timer is-active: $(systemctl is-active kite-auto-watchlist.timer 2>&1)"
  echo "kite-auto-watchlist.timer is-enabled: $(systemctl is-enabled kite-auto-watchlist.timer 2>&1)"
} >> $REPORT 2>&1

# ---------------- 6. DASHBOARD VERIFICATION ----------------
section 6 "DASHBOARD VERIFICATION"
{
  echo "-- Is anything listening on port 5000? --"
  sudo lsof -i :5000 2>&1
  echo ""
  echo "-- bot_status.json currently present? age? --"
  if [ -f bot_status.json ]; then
    ls -la bot_status.json
    python3 -c "import json; d=json.load(open('bot_status.json')); print('Last updated:', d.get('updated'))" 2>&1
  else
    echo "bot_status.json does not exist yet (clean slate)"
  fi
} >> $REPORT 2>&1

# ---------------- 8. CANDLE PROVIDER VERIFICATION ----------------
section 8 "CANDLE PROVIDER VERIFICATION"
{
  echo "-- show_active_features.py output --"
  python3 show_active_features.py 2>&1
  echo ""
  echo "-- Explicit execution-path determination --"
  python3 -c "
import config
if not config.ENABLE_WS_CANDLES:
    print('EXECUTION PATH: REST ONLY -- ENABLE_WS_CANDLES is False, candle_provider.augment_with_ws')
    print('  returns the REST result unmodified on every call, WS engine never even starts.')
elif config.WS_CANDLE_MODE == 'shadow':
    print('EXECUTION PATH: SHADOW MODE -- WS engine runs and logs comparisons, but')
    print('  candle_provider.augment_with_ws still returns REST-only data to the strategy.')
elif config.WS_CANDLE_MODE == 'live':
    print('EXECUTION PATH: LIVE AUGMENTATION -- candle_provider.augment_with_ws CAN splice')
    print('  a WS-built candle into the data the strategy evaluates.')
"
} >> $REPORT 2>&1

# ---------------- 10. ACCESS TOKEN VERIFICATION ----------------
section 10 "ACCESS TOKEN VERIFICATION"
{
  if [ -f access_token.txt ]; then
    ls -la access_token.txt
    echo "Token file exists. Zerodha access tokens expire daily (a fresh login via"
    echo "'python3 auth.py' is required each trading day regardless of file age --"
    echo "this is normal Kite Connect behavior, not specific to tonight's work)."
  else
    echo "access_token.txt NOT FOUND. A fresh 'python3 auth.py' login will be"
    echo "required before market open tomorrow."
  fi
} >> $REPORT 2>&1

# ---------------- 11. RISK CONTROLS ----------------
section 11 "RISK CONTROLS (config values only -- code paths verified unchanged in tonight's merge report)"
python3 -c "
import config
print('MAX_TRADES_PER_DAY =', getattr(config, 'MAX_TRADES_PER_DAY', '<<NOT SET>>'))
print('MAX_DAILY_LOSS_PCT =', getattr(config, 'MAX_DAILY_LOSS_PCT', '<<NOT SET>>'))
print('RISK_PER_TRADE_PCT =', getattr(config, 'RISK_PER_TRADE_PCT', '<<NOT SET>>'))
print('MAX_POSITION_SIZE_PCT =', getattr(config, 'MAX_POSITION_SIZE_PCT', '<<NOT SET>>'))
print('SL_BUFFER_PCT =', getattr(config, 'SL_BUFFER_PCT', '<<NOT SET>>'))
print('ENABLE_FIXED_TARGET =', getattr(config, 'ENABLE_FIXED_TARGET', '<<NOT SET>>'))
print('ENABLE_TRAILING_STOP =', getattr(config, 'ENABLE_TRAILING_STOP', '<<NOT SET>>'))
" >> $REPORT 2>&1

# ---------------- 12/13. LOGS AND HISTORICAL STATE ----------------
section 12 "LOGS AND HISTORICAL STATE"
{
  echo "-- Runtime state files currently present in this directory --"
  ls -la bot_status.json open_positions.json pending_orders.json trade_history.jsonl day_state.json protective_stops.json 2>&1
  echo ""
  echo "-- Untracked/ghost files from prior incidents (informational only) --"
  git status --porcelain | grep "^??"
} >> $REPORT 2>&1

echo "" >> $REPORT
echo "================================================================================" >> $REPORT
echo "Report written to $REPORT. Sections 7 (startup smoke test) and 9 (WebSocket" >> $REPORT
echo "live verification) are DELIBERATELY NOT run by this script -- see the separate" >> $REPORT
echo "safe-smoke-test instructions, since PAPER_TRADING=False means actually starting" >> $REPORT
echo "main.py here would begin real live trading immediately." >> $REPORT
echo "================================================================================" >> $REPORT

echo "DONE. Run: cat preflight_report.txt"
