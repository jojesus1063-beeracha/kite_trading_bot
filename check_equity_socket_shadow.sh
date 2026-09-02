#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="${1:-/home/ubuntu/kite_trading_bot}"
cd "$PROJECT"
TODAY_IST="$(TZ=Asia/Kolkata date +%F)"

echo "===== SERVICE STATE ====="
for unit in \
  kite-paper-watchlist.timer \
  kite-paper-watchlist.service \
  kitebot-paper-contrarian.service \
  kitebot-equity-socket-shadow.service \
  kite-live-watchlist.timer \
  kitebot-live-combined.service; do
  printf '%-42s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done

echo
echo "===== SAFETY AND PARAMETERS ====="
./venv/bin/python3 - <<'PY'
import config

values = {
    "PAPER_TRADING": config.PAPER_TRADING,
    "WATCHLIST_COUNT": len(config.WATCHLIST),
    "SOCKET_SHADOW_ENABLED": config.ENABLE_EQUITY_SOCKET_SHADOW,
    "RAW_TICK_RECORDING": config.SOCKET_SHADOW_RECORD_RAW_TICKS,
    "EMA_SIGNAL_DEPENDENCY": False,
    "VWAP_SIGNAL_DEPENDENCY": False,
    "SHADOW_CAPITAL_INR": config.SOCKET_SHADOW_CAPITAL,
    "OBSERVATION_SECONDS": config.SOCKET_SHADOW_OBSERVATION_SECONDS,
    "IMBALANCE_THRESHOLD": config.SOCKET_SHADOW_IMBALANCE_THRESHOLD,
    "PERSISTENCE_RATIO": config.SOCKET_SHADOW_PERSISTENCE_RATIO,
    "DIRECTIONAL_TICK_RATIO": config.SOCKET_SHADOW_DIRECTIONAL_TICK_RATIO,
    "MAX_SPREAD_PERCENT": config.SOCKET_SHADOW_MAX_SPREAD_PCT,
    "QUOTE_MAX_AGE_SECONDS": config.SOCKET_SHADOW_QUOTE_MAX_AGE_SECONDS,
    "STALE_TICK_SECONDS": config.SOCKET_SHADOW_TICK_MAX_AGE_SECONDS,
    "STALE_CONSECUTIVE_CHECKS": config.SOCKET_SHADOW_STALE_CONSECUTIVE_CHECKS,
    "STOP_PERCENT": config.SOCKET_SHADOW_STOP_PERCENT,
    "FRESH_EXIT_POLICY": "DYNAMIC_ADVERSE_ORDER_FLOW_ONLY",
    "FIXED_TARGETS_WHILE_FRESH": config.SOCKET_SHADOW_FIXED_TARGETS_ENABLED,
    "STALE_FALLBACK_POLICY": "0.45% SL + HALF@1R + HALF@2R; BE AFTER 1R",
    "STALE_FALLBACK_HYBRID": config.SOCKET_SHADOW_STALE_FALLBACK_HYBRID_ENABLED,
    "STALE_REST_POLL_SECONDS": config.SOCKET_SHADOW_STALE_REST_POLL_SECONDS,
    "MAX_OPEN_SHADOW_POSITIONS": config.SOCKET_SHADOW_MAX_OPEN_POSITIONS,
    "MAX_SHADOW_TRADES_PER_DAY": config.SOCKET_SHADOW_MAX_TRADES_PER_DAY,
    "MAX_TRADES_PER_SYMBOL": config.SOCKET_SHADOW_MAX_TRADES_PER_SYMBOL,
    "MAX_DAILY_LOSS_PERCENT": config.SOCKET_SHADOW_MAX_DAILY_LOSS_PCT,
    "DYNAMIC_EXIT_AUTHORITATIVE": config.SOCKET_SHADOW_DYNAMIC_EXIT_AUTHORITATIVE,
    "DYNAMIC_EXIT_WINDOW_SECONDS": config.SOCKET_SHADOW_DYNAMIC_EXIT_WINDOW_SECONDS,
    "DYNAMIC_EXIT_PERSIST_SECONDS": config.SOCKET_SHADOW_DYNAMIC_EXIT_PERSIST_SECONDS,
    "DYNAMIC_EXIT_IMBALANCE": config.SOCKET_SHADOW_DYNAMIC_EXIT_IMBALANCE,
}
for key, value in values.items():
    print(f"{key:32} = {value}")
PY

echo
echo "===== TODAY'S SHADOW OUTPUT ====="
OUTPUT="runtime/equity_socket_shadow"
SUMMARY="$OUTPUT/summary_${TODAY_IST}.json"
if [[ -f "$SUMMARY" ]]; then
  ./venv/bin/python3 -m json.tool "$SUMMARY"
else
  echo "No summary yet: $SUMMARY"
fi
ls -lh "$OUTPUT"/*"$TODAY_IST"* 2>/dev/null || true

echo
echo "===== RECENT SHADOW LOG ====="
sudo journalctl -u kitebot-equity-socket-shadow.service \
  --since "$TODAY_IST 00:00:00" --no-pager -l | tail -n 60
