"""Central configuration for the F&O opening-momentum options scalper.

This is a completely separate module from the equity bot's config.py -- nothing here is read by, or overrides, the equity bot, and nothing in the equity bot's user_config.json affects this file. See user_config_fno.json (optional, dashboard-managed override layer, mirroring the equity bot's pattern) for runtime overrides of the defaults below.

Every production-relevant parameter lives here, documented, with no magic numbers scattered through the rest of the codebase (spec #36).
"""

import os
import json

# ---------------------------------------------------------------------
# Broker credentials / auth
# ---------------------------------------------------------------------
API_KEY = os.environ.get("KITE_API_KEY", "your_api_key_here")
API_SECRET = os.environ.get("KITE_API_SECRET", "your_api_secret_here")
ACCESS_TOKEN_FILE = os.environ.get(
    "FNO_ACCESS_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "access_token.txt"),
)

# ---------------------------------------------------------------------
# Instrument scope
# ---------------------------------------------------------------------
UNDERLYING = os.environ.get("FNO_UNDERLYING", "SENSEX")
UNIVERSE_MODE = os.environ.get("FNO_UNIVERSE_MODE", "SINGLE").upper()
ALL_STOCK_OPTIONS_MAX_UNDERLYINGS = int(os.environ.get("FNO_STOCK_OPTION_LIMIT", "0"))
ALL_STOCK_OPTIONS_WEBSOCKET_LIMIT = 3000
ALL_STOCK_OPTIONS_LIVE_ENABLED = False

UNDERLYING_REGISTRY = {
    "SENSEX": {"exchange": "BFO", "index_exchange": "BSE", "index_symbol": "SENSEX", "strike_interval": 100},
    "NIFTY": {"exchange": "NFO", "index_exchange": "NSE", "index_symbol": "NIFTY 50", "strike_interval": 50},
    "BANKNIFTY": {"exchange": "NFO", "index_exchange": "NSE", "index_symbol": "NIFTY BANK", "strike_interval": 100},
}

PRODUCT = "MIS"
VARIETY = "regular"
ORDER_TYPE_ENTRY = "LIMIT"
MARKET_PROTECTION = -1

# ---------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------
MODE = os.environ.get("FNO_MODE", "PAPER")
FNO_LIVE_ACK_ENV_VAR = "FNO_LIVE_ACK"
FNO_LIVE_ACK_REQUIRED_VALUE = "I_ACCEPT_REAL_FNO_ORDERS"
PAPER_SLIPPAGE_PCT = 0.5

# Options-selling strategy: bullish underlying -> sell PE; bearish -> sell CE.
OPTION_STRATEGY = os.environ.get("FNO_OPTION_STRATEGY", "SELL_PREMIUM").upper()
SELL_OTM_STEPS = int(os.environ.get("FNO_SELL_OTM_STEPS", "1"))

# ---------------------------------------------------------------------
# Opening sequence / timing (all Asia/Kolkata, tz-aware)
# ---------------------------------------------------------------------
TIMEZONE = "Asia/Kolkata"
ENTRY_START_TIME = "09:25:00"
ENTRY_END_TIME = "14:30:00"
PREPARE_BEFORE_SECONDS = 600
FORCE_SQUARE_OFF_TIME = "15:30"

INTRADAY_OPTIONS_ENABLED = False
INTRADAY_ENTRY_START_TIME = "09:25:00"
INTRADAY_ENTRY_END_TIME = "14:30:00"
INTRADAY_FORCE_EXIT_TIME = "15:30:00"
INTRADAY_HISTORICAL_SHORTLIST_SIZE = 10
INTRADAY_HISTORICAL_CACHE_SECONDS = 55

# ---------------------------------------------------------------------
# Stale-data / connection-quality protection
# ---------------------------------------------------------------------
MAX_TICK_AGE_MS = 1500
MAX_SPREAD_PCT = 2.0
WEBSOCKET_RECONNECT_TIMEOUT_SECONDS = 10
DISCONNECT_WHILE_OPEN_RECOVERY_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------
ENTRY_BUFFER_PCT = 10.0
MAX_ENTRY_SLIPPAGE_PCT = 15.0
ENTRY_TIMEOUT_MS = 3000
MAX_ENTRY_ATTEMPTS = 3
ENTRY_RETRY_BACKOFF_MS = 250

# ---------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------
TARGET_PCT = 10.0
TARGET_PCT_CANDIDATES = [3.0, 5.0, 7.5, 10.0, 12.5, 15.0]
STOP_LOSS_PCT = 5.0
STOP_LOSS_PCT_CANDIDATES = [3.0, 5.0, 7.5]
MAX_LOSS_RUPEES = None
MAX_ADVERSE_MOVE_PCT = None
MAX_HOLD_SECONDS = 900
DYNAMIC_EXITS_ENABLED = False
MAX_ENTRY_WINDOW_SECONDS = 18600

EXIT_ORDER_BUFFER_PCT = 1.0
EXIT_REPRICE_WAIT_MS = 500
MAX_EXIT_REPRICE_ATTEMPTS = 4
EXIT_RETRY_INTERVAL_MS = 500

EXIT_PRIORITY_ORDER = [
    "EMERGENCY_RISK_EXIT",
    "HARD_STOP_LOSS",
    "SIGNAL_INVALIDATION",
    "PROFIT_TARGET",
    "TIME_STOP",
    "END_OF_SESSION_MANDATORY_EXIT",
]

# ---------------------------------------------------------------------
# Position sizing / capital
# ---------------------------------------------------------------------
FNO_CAPITAL = float(os.environ.get("FNO_TRADING_CAPITAL", "5000"))
MAX_CAPITAL_PER_TRADE_PCT = 100.0
MAX_RISK_PER_TRADE_PCT = 2.0
MAX_DAILY_LOSS = 1000.0
MAX_TRADES_PER_DAY = 2
MAX_CONSECUTIVE_LOSSES = 2
REENTRY_COOLDOWN_MINUTES = 20
MAX_CAPITAL_EXPOSURE_PCT = 100.0

# ---------------------------------------------------------------------
# Shared-capital coordination
# ---------------------------------------------------------------------
SHARED_CAPITAL_CHECK_ENABLED = True
SHARED_CAPITAL_LEDGER_PATH = os.environ.get(
    "FNO_SHARED_CAPITAL_LEDGER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared_capital_ledger.json"),
)

# ---------------------------------------------------------------------
# Order safety / duplicate protection
# ---------------------------------------------------------------------
ORDER_VERIFY_MAX_WAIT_SECONDS = 8
ORDER_VERIFY_POLL_INTERVAL_SECONDS = 0.5
MAX_ORDER_RETRIES = 3

# ---------------------------------------------------------------------
# Directional signal selection
# ---------------------------------------------------------------------
AUTHORIZED_SIGNAL = None
SHADOW_SIGNAL_CANDIDATES = [
    "premium_imbalance",
    "premium_rate_of_change",
    "underlying_open_vs_prev_close",
    "bid_ask_imbalance",
    "depth_imbalance",
]
SIGNAL_CONFIRMATION_WINDOW_MS = 3000
COUNTERFACTUAL_HORIZONS_SECONDS = [1, 2, 5, 10, 30, 60]

# ---------------------------------------------------------------------
# Options-specific entry/exit tuning
# ---------------------------------------------------------------------
FNO_WINDOW_SECONDS = float(os.environ.get("FNO_WINDOW_SECONDS", "60"))
FNO_CONFIRMATION_COUNT = int(os.environ.get("FNO_CONFIRMATION_COUNT", "3"))
FNO_SCORE_THRESHOLD = float(os.environ.get("FNO_SCORE_THRESHOLD", "68"))
FNO_DOMINANCE_MARGIN = float(os.environ.get("FNO_DOMINANCE_MARGIN", "15"))
FNO_ANTI_CHASE_LOOKBACK_SECONDS = float(os.environ.get("FNO_ANTI_CHASE_LOOKBACK_SECONDS", "30"))
FNO_ANTI_CHASE_MAX_EXTENSION_PCT = float(os.environ.get("FNO_ANTI_CHASE_MAX_EXTENSION_PCT", "10"))
FNO_HARD_STOP_PCT = float(os.environ.get("FNO_HARD_STOP_PCT", "12"))
FNO_PROFIT_TARGET_ENABLED = os.environ.get("FNO_PROFIT_TARGET_ENABLED", "false").lower() == "true"
FNO_TRAILING_ACTIVATION_PCT = float(os.environ.get("FNO_TRAILING_ACTIVATION_PCT", "8"))
FNO_TRAILING_DISTANCE_PCT = float(os.environ.get("FNO_TRAILING_DISTANCE_PCT", "4"))
FNO_TIME_STOP_SECONDS = float(os.environ.get("FNO_TIME_STOP_SECONDS", "900"))
FNO_TIME_STOP_MIN_PROGRESS_PCT = float(os.environ.get("FNO_TIME_STOP_MIN_PROGRESS_PCT", "1"))
FNO_MOMENTUM_EXIT_MIN_PROFIT_PCT = float(os.environ.get("FNO_MOMENTUM_EXIT_MIN_PROFIT_PCT", "3"))
FNO_STRUCTURE_CONFIRMATIONS = int(os.environ.get("FNO_STRUCTURE_CONFIRMATIONS", "2"))
FNO_LATE_ENTRY_CUTOFF = os.environ.get("FNO_LATE_ENTRY_CUTOFF", "14:30")
FNO_EXIT_CUTOFF = os.environ.get("FNO_EXIT_CUTOFF", "15:30")
FNO_PAPER_ONLY_DEFAULT = True

# ---------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------
DEBUG_TICK_LOGGING = False

# ---------------------------------------------------------------------
# Optional F&O UI overrides
# ---------------------------------------------------------------------
_USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config_fno.json")

if os.path.exists(_USER_CONFIG_PATH):
    with open(_USER_CONFIG_PATH) as _f:
        _overrides = json.load(_f)

    UNDERLYING = _overrides.get("underlying", UNDERLYING)
    UNIVERSE_MODE = _overrides.get("universe_mode", UNIVERSE_MODE).upper()
    MODE = _overrides.get("mode", MODE)
    OPTION_STRATEGY = _overrides.get("option_strategy", OPTION_STRATEGY).upper()
    SELL_OTM_STEPS = int(_overrides.get("sell_otm_steps", SELL_OTM_STEPS))
    ENTRY_START_TIME = _overrides.get("entry_start_time", ENTRY_START_TIME)
    ENTRY_END_TIME = _overrides.get("entry_end_time", ENTRY_END_TIME)
    INTRADAY_OPTIONS_ENABLED = _overrides.get("intraday_options_enabled", INTRADAY_OPTIONS_ENABLED)
    INTRADAY_ENTRY_START_TIME = _overrides.get("intraday_entry_start_time", INTRADAY_ENTRY_START_TIME)
    INTRADAY_ENTRY_END_TIME = _overrides.get("intraday_entry_end_time", INTRADAY_ENTRY_END_TIME)
    INTRADAY_FORCE_EXIT_TIME = _overrides.get("intraday_force_exit_time", INTRADAY_FORCE_EXIT_TIME)
    INTRADAY_HISTORICAL_SHORTLIST_SIZE = _overrides.get("intraday_historical_shortlist_size", INTRADAY_HISTORICAL_SHORTLIST_SIZE)
    ENTRY_BUFFER_PCT = _overrides.get("entry_buffer_pct", ENTRY_BUFFER_PCT)
    MAX_ENTRY_SLIPPAGE_PCT = _overrides.get("max_entry_slippage_pct", MAX_ENTRY_SLIPPAGE_PCT)
    ENTRY_TIMEOUT_MS = _overrides.get("entry_timeout_ms", ENTRY_TIMEOUT_MS)
    MAX_ENTRY_ATTEMPTS = _overrides.get("max_entry_attempts", MAX_ENTRY_ATTEMPTS)
    TARGET_PCT = _overrides.get("target_pct", TARGET_PCT)
    STOP_LOSS_PCT = _overrides.get("stop_loss_pct", STOP_LOSS_PCT)
    MAX_HOLD_SECONDS = _overrides.get("max_hold_seconds", MAX_HOLD_SECONDS)
    DYNAMIC_EXITS_ENABLED = _overrides.get("dynamic_exits_enabled", DYNAMIC_EXITS_ENABLED)
    MAX_TICK_AGE_MS = _overrides.get("max_tick_age_ms", MAX_TICK_AGE_MS)
    MAX_SPREAD_PCT = _overrides.get("max_spread_pct", MAX_SPREAD_PCT)
    FNO_CAPITAL = _overrides.get("fno_capital", FNO_CAPITAL)
    MAX_CAPITAL_PER_TRADE_PCT = _overrides.get("max_capital_per_trade_pct", MAX_CAPITAL_PER_TRADE_PCT)
    MAX_RISK_PER_TRADE_PCT = _overrides.get("max_risk_per_trade_pct", MAX_RISK_PER_TRADE_PCT)
    MAX_DAILY_LOSS = _overrides.get("max_daily_loss", MAX_DAILY_LOSS)
    MAX_TRADES_PER_DAY = _overrides.get("max_trades_per_day", MAX_TRADES_PER_DAY)
    MAX_CONSECUTIVE_LOSSES = _overrides.get("max_consecutive_losses", MAX_CONSECUTIVE_LOSSES)
    REENTRY_COOLDOWN_MINUTES = _overrides.get("reentry_cooldown_minutes", REENTRY_COOLDOWN_MINUTES)
    AUTHORIZED_SIGNAL = _overrides.get("authorized_signal", AUTHORIZED_SIGNAL)
    DEBUG_TICK_LOGGING = _overrides.get("debug_tick_logging", DEBUG_TICK_LOGGING)


def is_live_ack_present() -> bool:
    return os.environ.get(FNO_LIVE_ACK_ENV_VAR) == FNO_LIVE_ACK_REQUIRED_VALUE


def validate_mode():
    if MODE not in ("SHADOW", "PAPER", "LIVE"):
        raise RuntimeError(f"Invalid FNO_MODE={MODE!r}; must be SHADOW, PAPER, or LIVE")
    if UNIVERSE_MODE not in ("SINGLE", "ALL_STOCK_OPTIONS"):
        raise RuntimeError(
            f"Invalid FNO_UNIVERSE_MODE={UNIVERSE_MODE!r}; must be SINGLE or ALL_STOCK_OPTIONS"
        )
    if MODE == "LIVE" and not is_live_ack_present():
        raise RuntimeError(
            f"REFUSING TO START LIVE TRADING: environment variable {FNO_LIVE_ACK_ENV_VAR} "
            f"must be set to exactly {FNO_LIVE_ACK_REQUIRED_VALUE!r}. "
            f"Set FNO_MODE=SHADOW or FNO_MODE=PAPER instead, or provide the acknowledgement."
        )
