"""
Central configuration for the intraday candle+MA trading bot.

Fill in your own values below. NEVER commit real API keys/secrets to a
public repo — use environment variables in production.
"""

import os

# ---------------------------------------------------------------------
# Kite Connect credentials
# ---------------------------------------------------------------------
API_KEY = os.environ.get("KITE_API_KEY", "your_api_key_here")
API_SECRET = os.environ.get("KITE_API_SECRET", "your_api_secret_here")
ACCESS_TOKEN_FILE = "access_token.txt"

# ---------------------------------------------------------------------
# Instruments to trade (cash intraday / MIS)
# ---------------------------------------------------------------------
WATCHLIST = [
    {"symbol": "RELIANCE", "exchange": "NSE"},
    {"symbol": "TCS", "exchange": "NSE"},
    {"symbol": "HDFCBANK", "exchange": "NSE"},
    {"symbol": "INFY", "exchange": "NSE"},
]

# ---------------------------------------------------------------------
# Strategy timeframes
# ---------------------------------------------------------------------
TREND_TIMEFRAME = "15minute"
ENTRY_TIMEFRAME = "5minute"

# ---------------------------------------------------------------------
# Indicator settings
# ---------------------------------------------------------------------
TREND_EMA_FAST = 20
TREND_EMA_SLOW = 50
ENTRY_EMA = 20
VOLUME_LOOKBACK = 20
VOLUME_MULTIPLIER = 1.2

USE_ADX_FILTER = False
ADX_PERIOD = 14
ADX_THRESHOLD = 25
ADX_MODE = ""
ADX_DYNAMIC_MIN = 20
ADX_DYNAMIC_STRONG = 35

# ---------------------------------------------------------------------
# Risk management -- stewardship defaults
# ---------------------------------------------------------------------
# Preserve capital first. A normal trade risks 0.20% of configured
# capital and the entire day is capped at 0.50%. The prospective risk
# budget in risk_manager.py also includes open stop risk before a new
# order may be submitted.
RISK_REWARD_MIN = 2.0
RISK_PER_TRADE_PCT = 0.20
MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 1
MAX_POSITION_SIZE_PCT = 50.0
CHECK_MARGIN_BEFORE_ENTRY = True
MAX_DAILY_LOSS_PCT = 0.50
CAPITAL = float(os.environ.get("TRADING_CAPITAL", "100000"))

SL_BUFFER_PCT = 0.05
SL_BUFFER_PCT_SELL = None

CIRCUIT_PROXIMITY_PCT = 2.0

ENABLE_MARKET_ALIGNMENT_FILTER = False

MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY", "")
ENABLE_NEWS_FILTER = False
NEWS_LOOKBACK_HOURS = 24
NEWS_CACHE_MINUTES = 5
NEWS_TIMEOUT_SECONDS = 2
NEGATIVE_NEWS_BLOCK = True
POSITIVE_NEWS_CONFIDENCE_BONUS = 5
NEGATIVE_NEWS_CONFIDENCE_PENALTY = 25

ENABLE_PRICE_ACTION = False
USE_MARKET_STRUCTURE = True
USE_SUPPORT_RESISTANCE = True
USE_BREAKOUT_CONFIRMATION = True
USE_PULLBACK_ENTRY = True
USE_REJECTION_CANDLES = True
USE_RANGE_FILTER = True
USE_BOS = True
USE_CHOCH = True
SUPPORT_RESISTANCE_LOOKBACK = 30
MIN_DISTANCE_TO_SR_PERCENT = 0.5

# Fixed percentage targets are disabled by default because they can
# silently reduce a strategy-approved 2R setup below 2R. The strategy
# target therefore remains authoritative unless a caller explicitly
# enables a fixed target and also preserves the minimum R:R.
PROFIT_TARGET_PERCENT = 1.50
ENABLE_FIXED_TARGET = False
ENABLE_TRAILING_STOP = True
EXIT_IMMEDIATELY_AT_TARGET = True

# Two completed adverse candles are required for the new confirmation
# exit path when the live orchestrator enables it.
ADVERSE_EXIT_CONFIRM_CANDLES = 2

# Optional consolidated quality gate. The live orchestrator may add
# market alignment / price-action / news evidence to the technical
# confidence score and reject below this threshold.
ENABLE_ENTRY_QUALITY_GATE = True
MIN_ENTRY_QUALITY_SCORE = 70

# ---------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------
PRODUCT = "MIS"
ORDER_TYPE_ENTRY = "MARKET"
VARIETY = "regular"
MARKET_PROTECTION = -1
FORCE_SQUARE_OFF_TIME = "15:10"
NO_ENTRY_BEFORE = "09:25"
NO_ENTRY_AFTER = "15:00"
PAPER_TRADING = True

# ---------------------------------------------------------------------
# Candle-aligned scheduler
# ---------------------------------------------------------------------
ENABLE_CANDLE_ALIGNED_POLLING = False
POSITION_CHECK_SECONDS = 25
SCAN_BUFFER_SECONDS = 8
SCHEDULER_WARNING_SCAN_SECONDS = 90
SCHEDULER_CRITICAL_SCAN_SECONDS = 120
POSITION_CHECK_WARNING_SECONDS = 40
POSITION_CHECK_CRITICAL_SECONDS = 60
SCAN_DELAY_WARNING_SECONDS = 30
SCAN_DELAY_CRITICAL_SECONDS = 60

# ---------------------------------------------------------------------
# Overrides from the web configuration UI (configure_app.py)
# ---------------------------------------------------------------------
import json

_USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")

if os.path.exists(_USER_CONFIG_PATH):
    with open(_USER_CONFIG_PATH) as _f:
        _overrides = json.load(_f)

    _saved_watchlist = _overrides.get("watchlist")
    if _saved_watchlist is not None:
        WATCHLIST = [
            {"symbol": w, "exchange": "NSE"} if isinstance(w, str) else w
            for w in _saved_watchlist
        ]

    CAPITAL = _overrides.get("capital", CAPITAL)
    RISK_PER_TRADE_PCT = _overrides.get("risk_per_trade_pct", RISK_PER_TRADE_PCT)
    RISK_REWARD_MIN = _overrides.get("risk_reward_min", RISK_REWARD_MIN)
    SL_BUFFER_PCT = _overrides.get("sl_buffer_pct", SL_BUFFER_PCT)
    SL_BUFFER_PCT_SELL = _overrides.get("sl_buffer_pct_sell", SL_BUFFER_PCT_SELL)
    CIRCUIT_PROXIMITY_PCT = _overrides.get("circuit_proximity_pct", CIRCUIT_PROXIMITY_PCT)
    ENABLE_MARKET_ALIGNMENT_FILTER = _overrides.get("enable_market_alignment_filter", ENABLE_MARKET_ALIGNMENT_FILTER)
    ENABLE_NEWS_FILTER = _overrides.get("enable_news_filter", ENABLE_NEWS_FILTER)
    NEWS_LOOKBACK_HOURS = _overrides.get("news_lookback_hours", NEWS_LOOKBACK_HOURS)
    NEWS_CACHE_MINUTES = _overrides.get("news_cache_minutes", NEWS_CACHE_MINUTES)
    NEWS_TIMEOUT_SECONDS = _overrides.get("news_timeout_seconds", NEWS_TIMEOUT_SECONDS)
    NEGATIVE_NEWS_BLOCK = _overrides.get("negative_news_block", NEGATIVE_NEWS_BLOCK)
    POSITIVE_NEWS_CONFIDENCE_BONUS = _overrides.get("positive_news_confidence_bonus", POSITIVE_NEWS_CONFIDENCE_BONUS)
    NEGATIVE_NEWS_CONFIDENCE_PENALTY = _overrides.get("negative_news_confidence_penalty", NEGATIVE_NEWS_CONFIDENCE_PENALTY)
    ENABLE_PRICE_ACTION = _overrides.get("enable_price_action", ENABLE_PRICE_ACTION)
    USE_MARKET_STRUCTURE = _overrides.get("use_market_structure", USE_MARKET_STRUCTURE)
    USE_SUPPORT_RESISTANCE = _overrides.get("use_support_resistance", USE_SUPPORT_RESISTANCE)
    USE_BREAKOUT_CONFIRMATION = _overrides.get("use_breakout_confirmation", USE_BREAKOUT_CONFIRMATION)
    USE_PULLBACK_ENTRY = _overrides.get("use_pullback_entry", USE_PULLBACK_ENTRY)
    USE_REJECTION_CANDLES = _overrides.get("use_rejection_candles", USE_REJECTION_CANDLES)
    USE_RANGE_FILTER = _overrides.get("use_range_filter", USE_RANGE_FILTER)
    USE_BOS = _overrides.get("use_bos", USE_BOS)
    USE_CHOCH = _overrides.get("use_choch", USE_CHOCH)
    SUPPORT_RESISTANCE_LOOKBACK = _overrides.get("support_resistance_lookback", SUPPORT_RESISTANCE_LOOKBACK)
    MIN_DISTANCE_TO_SR_PERCENT = _overrides.get("min_distance_to_sr_percent", MIN_DISTANCE_TO_SR_PERCENT)
    PROFIT_TARGET_PERCENT = _overrides.get("profit_target_percent", PROFIT_TARGET_PERCENT)
    NO_ENTRY_BEFORE = _overrides.get("no_entry_before", NO_ENTRY_BEFORE)
    NO_ENTRY_AFTER = _overrides.get("no_entry_after", NO_ENTRY_AFTER)
    ENABLE_FIXED_TARGET = _overrides.get("enable_fixed_target", ENABLE_FIXED_TARGET)
    ENABLE_TRAILING_STOP = _overrides.get("enable_trailing_stop", ENABLE_TRAILING_STOP)
    EXIT_IMMEDIATELY_AT_TARGET = _overrides.get("exit_immediately_at_target", EXIT_IMMEDIATELY_AT_TARGET)
    MAX_TRADES_PER_DAY = _overrides.get("max_trades_per_day", MAX_TRADES_PER_DAY)
    MAX_OPEN_POSITIONS = _overrides.get("max_open_positions", MAX_OPEN_POSITIONS)
    MAX_POSITION_SIZE_PCT = _overrides.get("max_position_size_pct", MAX_POSITION_SIZE_PCT)
    CHECK_MARGIN_BEFORE_ENTRY = _overrides.get("check_margin_before_entry", CHECK_MARGIN_BEFORE_ENTRY)
    MAX_DAILY_LOSS_PCT = _overrides.get("max_daily_loss_pct", MAX_DAILY_LOSS_PCT)
    TREND_EMA_FAST = _overrides.get("trend_ema_fast", TREND_EMA_FAST)
    TREND_EMA_SLOW = _overrides.get("trend_ema_slow", TREND_EMA_SLOW)
    ENTRY_EMA = _overrides.get("entry_ema", ENTRY_EMA)
    PAPER_TRADING = _overrides.get("paper_trading", PAPER_TRADING)
    USE_ADX_FILTER = _overrides.get("use_adx_filter", USE_ADX_FILTER)
    ADX_THRESHOLD = _overrides.get("adx_threshold", ADX_THRESHOLD)
    ADX_MODE = _overrides.get("adx_mode", ADX_MODE)
    ADX_DYNAMIC_MIN = _overrides.get("adx_dynamic_min", ADX_DYNAMIC_MIN)
    ADX_DYNAMIC_STRONG = _overrides.get("adx_dynamic_strong", ADX_DYNAMIC_STRONG)
    ENABLE_CANDLE_ALIGNED_POLLING = _overrides.get("enable_candle_aligned_polling", ENABLE_CANDLE_ALIGNED_POLLING)
    POSITION_CHECK_SECONDS = _overrides.get("position_check_seconds", POSITION_CHECK_SECONDS)
    SCAN_BUFFER_SECONDS = _overrides.get("scan_buffer_seconds", SCAN_BUFFER_SECONDS)
    SCHEDULER_WARNING_SCAN_SECONDS = _overrides.get("scheduler_warning_scan_seconds", SCHEDULER_WARNING_SCAN_SECONDS)
    SCHEDULER_CRITICAL_SCAN_SECONDS = _overrides.get("scheduler_critical_scan_seconds", SCHEDULER_CRITICAL_SCAN_SECONDS)
    POSITION_CHECK_WARNING_SECONDS = _overrides.get("position_check_warning_seconds", POSITION_CHECK_WARNING_SECONDS)
    POSITION_CHECK_CRITICAL_SECONDS = _overrides.get("position_check_critical_seconds", POSITION_CHECK_CRITICAL_SECONDS)
    SCAN_DELAY_WARNING_SECONDS = _overrides.get("scan_delay_warning_seconds", SCAN_DELAY_WARNING_SECONDS)
    SCAN_DELAY_CRITICAL_SECONDS = _overrides.get("scan_delay_critical_seconds", SCAN_DELAY_CRITICAL_SECONDS)
    ADVERSE_EXIT_CONFIRM_CANDLES = _overrides.get("adverse_exit_confirm_candles", ADVERSE_EXIT_CONFIRM_CANDLES)
    ENABLE_ENTRY_QUALITY_GATE = _overrides.get("enable_entry_quality_gate", ENABLE_ENTRY_QUALITY_GATE)
    MIN_ENTRY_QUALITY_SCORE = _overrides.get("min_entry_quality_score", MIN_ENTRY_QUALITY_SCORE)
