#!/bin/bash
set -eu
cd /home/ubuntu/kite_trading_bot
for attempt in {1..12}; do
  now=$(TZ=Asia/Kolkata date +%H%M%S)
  if [[ "$now" < "090000" || "$now" > "091759" ]]; then
    echo 'Outside pre-open selector window; preserving watchlist.'
    exit 1
  fi
  if venv/bin/python3 matmon_preopen_top120.py; then exit 0; fi
  sleep 45
done
exit 1
