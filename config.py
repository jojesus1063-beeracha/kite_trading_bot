"""
Central configuration for the intraday candle+MA trading bot.

Fill in your own values below. NEVER commit real API keys/secrets to a
public repo — use environment variables in production (see the
os.environ examples, commented out).
"""

import os

# ---------------------------------------------------------------------
# Kite Connect credentials
# ---------------------------------------------------------------------
# Get these from https://developers.kite.trade after subscribing to
# Kite Connect (paid, separate from your regular trading plan).
API_KEY = os.environ.get("KITE_API_KEY", "your_api_key_here")
API_SECRET = os.environ.get("KITE_API_SECRET", "your_api_secret_here")

# Kite access tokens expire daily. request_token is generated fresh each
# morning via the login flow in auth.py — see README for the manual
# step required (Kite Connect has no fully headless login).
ACCESS_TOKEN_FILE = "access_token.txt"

# ---------------------------------------------------------------------
# Instruments to trade (cash intraday / MIS)
# ---------------------------------------------------------------------
# Each entry has its own exchange, so you can mix NSE and BSE stocks
# in the same watchlist.
WATCHLIST = [
    {"symbol": "RELIANCE", "exchange": "NSE"},
    {"symbol": "TCS", "exchange": "NSE"},
    {"symbol": "HDFCBANK", "exchange": "NSE"},
    {"symbol": "INFY", "exchange": "NSE"},
]

# ---------------------------------------------------------------------
# Strategy timeframes
# ---------------------------------------------------------------------
TREND_TIMEFRAME = "15minute"   # Kite historical API interval string
ENTRY_TIMEFRAME = "5minute"

# ---------------------------------------------------------------------
# Indicator settings
# ---------------------------------------------------------------------
TREND_EMA_FAST = 20     # on 15-min chart
TREND_EMA_SLOW = 50     # on 15-min chart
ENTRY_EMA = 20          # on 5-min chart
VOLUME_LOOKBACK = 20    # bars, for average-volume comparison on 5-min chart
VOLUME_MULTIPLIER = 1.2  # entry candle volume must exceed avg volume * this

# ADX (Average Directional Index) — measures trend STRENGTH (0-100),
# used as an optional filter on top of the existing EMA/VWAP trend
# check. When enabled, a 15-min candle only counts as trending if ADX
# is above the threshold — filters out choppy periods where price
# happens to sit above/below the EMAs without a real trend behind it.
# Toggle this to False to compare strategy performance with/without.
USE_ADX_FILTER = False
ADX_PERIOD = 14
ADX_THRESHOLD = 25  # Wilder's original suggested threshold for "trending"

# ---------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------
RISK_REWARD_MIN = 2.0          # minimum reward:risk ratio (1:2)
RISK_PER_TRADE_PCT = 1.0       # % of capital risked per trade
MAX_TRADES_PER_DAY = 4
MAX_DAILY_LOSS_PCT = 3.0       # kill-switch: stop trading if daily loss exceeds this %
CAPITAL = float(os.environ.get("TRADING_CAPITAL", "100000"))  # your intraday capital, INR

# Stop-loss is placed at the low (long) / high (short) of the signal
# candle, minus/plus a small buffer to avoid getting stopped out by
# noise.
SL_BUFFER_PCT = 0.05  # 0.05% buffer beyond the signal candle's extreme

# ---------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------
PRODUCT = "MIS"          # intraday margin product
ORDER_TYPE_ENTRY = "MARKET"
VARIETY = "regular"

# SEBI/NSE's Apr-2026 rules require every MARKET (and SL-M) order placed via
# the API to include market_protection, or it gets rejected outright.
# -1 = automatic protection applied by the exchange; or set 1-100 for a
# specific percentage band. Requires kiteconnect Python SDK >= 5.2.0.
MARKET_PROTECTION = -1

# Square-off: MIS positions must be closed before this time regardless
# of strategy signals (broker auto-square-off is usually ~15:15-15:20;
# closing a bit earlier avoids slippage/rejections near the deadline).
FORCE_SQUARE_OFF_TIME = "15:10"

# Trading window — don't take new entries in the first/last few minutes
# of the session (high volatility / low liquidity for stops).
NO_ENTRY_BEFORE = "09:25"
NO_ENTRY_AFTER = "15:00"

# Live vs paper mode. ALWAYS start with PAPER_TRADING = True.
PAPER_TRADING = True

# ---------------------------------------------------------------------
# Overrides from the web configuration UI (configure_app.py)
# ---------------------------------------------------------------------
# Settings changed via the browser form are saved to user_config.json
# and applied here, on top of the defaults above. Delete that file to
# fall back to the hardcoded defaults.
import json

_USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")

if os.path.exists(_USER_CONFIG_PATH):
    with open(_USER_CONFIG_PATH) as _f:
        _overrides = json.load(_f)

    _saved_watchlist = _overrides.get("watchlist")
    if _saved_watchlist is not None:
        # Support both the old format (plain symbol strings, all NSE)
        # and the new format (dicts with an exchange per symbol).
        WATCHLIST = [
            {"symbol": w, "exchange": "NSE"} if isinstance(w, str) else w
            for w in _saved_watchlist
        ]

    CAPITAL = _overrides.get("capital", CAPITAL)
    RISK_PER_TRADE_PCT = _overrides.get("risk_per_trade_pct", RISK_PER_TRADE_PCT)
    RISK_REWARD_MIN = _overrides.get("risk_reward_min", RISK_REWARD_MIN)
    SL_BUFFER_PCT = _overrides.get("sl_buffer_pct", SL_BUFFER_PCT)
    MAX_TRADES_PER_DAY = _overrides.get("max_trades_per_day", MAX_TRADES_PER_DAY)
    MAX_DAILY_LOSS_PCT = _overrides.get("max_daily_loss_pct", MAX_DAILY_LOSS_PCT)
    TREND_EMA_FAST = _overrides.get("trend_ema_fast", TREND_EMA_FAST)
    TREND_EMA_SLOW = _overrides.get("trend_ema_slow", TREND_EMA_SLOW)
    ENTRY_EMA = _overrides.get("entry_ema", ENTRY_EMA)
    PAPER_TRADING = _overrides.get("paper_trading", PAPER_TRADING)
    USE_ADX_FILTER = _overrides.get("use_adx_filter", USE_ADX_FILTER)
    ADX_THRESHOLD = _overrides.get("adx_threshold", ADX_THRESHOLD)
