"""
Central configuration for the F&O opening-momentum options scalper.

This is a completely separate module from the equity bot's config.py --
nothing here is read by, or overrides, the equity bot, and nothing in
the equity bot's user_config.json affects this file. See
user_config_fno.json (optional, dashboard-managed override layer,
mirroring the equity bot's pattern) for runtime overrides of the
defaults below.

Every production-relevant parameter lives here, documented, with no
magic numbers scattered through the rest of the codebase (spec #36).
"""

import os
import json

# ---------------------------------------------------------------------
# Broker credentials / auth
# ---------------------------------------------------------------------
# Shares ONE Kite Connect login with the equity bot -- same account,
# same daily access token. This bot never regenerates the token itself;
# it only reads the file the equity bot's auth.py (or a standalone run
# of it) already produced. Path is intentionally identical to the
# equity repo's ACCESS_TOKEN_FILE so both processes read the same file.
API_KEY = os.environ.get("KITE_API_KEY", "your_api_key_here")
API_SECRET = os.environ.get("KITE_API_SECRET", "your_api_secret_here")
ACCESS_TOKEN_FILE = os.environ.get(
    "FNO_ACCESS_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "access_token.txt"),
)

# ---------------------------------------------------------------------
# Instrument scope
# ---------------------------------------------------------------------
# Initial scope is SENSEX only (BSE F&O / "BFO" segment). Architecture
# supports adding NIFTY/BANKNIFTY (NSE F&O / "NFO" segment) later by
# adding entries here -- nothing else in the codebase should hardcode
# "SENSEX" or "BFO" once instruments/ is fully wired.
UNDERLYING = os.environ.get("FNO_UNDERLYING", "SENSEX")

# SINGLE keeps the original index-only launcher behavior. ALL_STOCK_OPTIONS
# dynamically discovers every NSE equity with listed NFO options and scans
# only its nearest-expiry ATM CE/PE pair. The feature is opt-in so adding it
# cannot silently broaden an existing deployment.
UNIVERSE_MODE = os.environ.get("FNO_UNIVERSE_MODE", "SINGLE").upper()
ALL_STOCK_OPTIONS_MAX_UNDERLYINGS = int(os.environ.get("FNO_STOCK_OPTION_LIMIT", "0"))  # 0 = all
ALL_STOCK_OPTIONS_WEBSOCKET_LIMIT = 3000
ALL_STOCK_OPTIONS_LIVE_ENABLED = False  # PAPER/SHADOW validation is mandatory first

UNDERLYING_REGISTRY = {
    "SENSEX": {"exchange": "BFO", "index_exchange": "BSE", "index_symbol": "SENSEX", "strike_interval": 100},
    "NIFTY": {"exchange": "NFO", "index_exchange": "NSE", "index_symbol": "NIFTY 50", "strike_interval": 50},
    "BANKNIFTY": {"exchange": "NFO", "index_exchange": "NSE", "index_symbol": "NIFTY BANK", "strike_interval": 100},
}

PRODUCT = "MIS"          # intraday margin product -- no overnight positions in V1
VARIETY = "regular"
ORDER_TYPE_ENTRY = "LIMIT"   # aggressive limit buy, not MARKET -- spec #7
MARKET_PROTECTION = -1        # -1 = exchange-automatic protection band (mandatory since Apr-2026)

# ---------------------------------------------------------------------
# Mode: SHADOW (signals + counterfactuals only, no orders) / PAPER
# (simulated fills) / LIVE (real broker orders). ALWAYS start SHADOW.
# ---------------------------------------------------------------------
MODE = os.environ.get("FNO_MODE", "SHADOW")  # SHADOW | PAPER | LIVE

# LIVE mode refuses to start without this exact acknowledgement string
# present in the environment -- never silently falls back from
# PAPER/SHADOW to LIVE (spec #23).
FNO_LIVE_ACK_ENV_VAR = "FNO_LIVE_ACK"
FNO_LIVE_ACK_REQUIRED_VALUE = "I_ACCEPT_REAL_FNO_ORDERS"

# Paper-mode fill simulation
PAPER_SLIPPAGE_PCT = 0.5   # simulated adverse slippage applied to paper fills, % of reference price

# ---------------------------------------------------------------------
# Opening sequence / timing (all Asia/Kolkata, tz-aware)
# ---------------------------------------------------------------------
TIMEZONE = "Asia/Kolkata"

ENTRY_START_TIME = "09:15:00"   # earliest the strategy may act on a signal
ENTRY_END_TIME = "09:20:00"     # opening-momentum window closes -- no fresh FIRST_TICK entries after this
                                  # (existing open positions are unaffected; see exit hierarchy)
PREPARE_BEFORE_SECONDS = 300     # start PREPARE (auth, contract master, ws connect+subscribe) this many
                                   # seconds before ENTRY_START_TIME, so nothing time-critical happens at 09:15:00 itself

FORCE_SQUARE_OFF_TIME = "15:10"  # mandatory end-of-session exit, same discipline as equity bot

# ---------------------------------------------------------------------
# Stale-data / connection-quality protection (spec #25, #26)
# ---------------------------------------------------------------------
MAX_TICK_AGE_MS = 1500            # reject a tick older than this for any trading decision
MAX_SPREAD_PCT = 3.0              # reject entries when bid/ask spread exceeds this % of mid
WEBSOCKET_RECONNECT_TIMEOUT_SECONDS = 10   # max time to re-establish + resubscribe before STOP_NEW_ENTRIES
DISCONNECT_WHILE_OPEN_RECOVERY_TIMEOUT_SECONDS = 30  # max time to reconcile against broker before EMERGENCY_EXIT

# ---------------------------------------------------------------------
# Entry (spec #7-9)
# ---------------------------------------------------------------------
ENTRY_BUFFER_PCT = 10.0           # aggressive limit = reference_price * (1 + buffer/100); NOT assumed optimal,
                                    # shadow/backtest before trusting this default
MAX_ENTRY_SLIPPAGE_PCT = 15.0     # hard ceiling -- if the executable price implies slippage beyond this
                                    # vs the original reference price, ABORT ENTRY rather than chase further
ENTRY_TIMEOUT_MS = 3000           # max wait for one entry attempt's fill confirmation
MAX_ENTRY_ATTEMPTS = 3
ENTRY_RETRY_BACKOFF_MS = 250      # pause between attempts, each attempt re-reads fresh depth/LTP/spread first

# ---------------------------------------------------------------------
# Exit (spec #12-16)
# ---------------------------------------------------------------------
TARGET_PCT = 10.0                 # profit target as % of ACTUAL fill price -- not assumed optimal,
                                    # candidate values for shadow/backtest comparison: see TARGET_PCT_CANDIDATES
TARGET_PCT_CANDIDATES = [3.0, 5.0, 7.5, 10.0, 12.5, 15.0]

STOP_LOSS_PCT = 5.0               # hard SL as % of ACTUAL fill price -- mandatory, never optional
STOP_LOSS_PCT_CANDIDATES = [3.0, 5.0, 7.5]
MAX_LOSS_RUPEES = None            # optional absolute rupee cap per trade, in addition to STOP_LOSS_PCT; None = unused
MAX_ADVERSE_MOVE_PCT = None       # optional secondary/emergency threshold beyond STOP_LOSS_PCT; None = unused

MAX_HOLD_SECONDS = 90             # time stop -- exit if opening momentum hasn't produced target/SL by then
MAX_ENTRY_WINDOW_SECONDS = 300    # if no valid signal has fired this long after ENTRY_START_TIME, stop trying for the day

EXIT_ORDER_BUFFER_PCT = 1.0       # initial exit limit = best_bid * (1 - buffer/100), i.e. slightly aggressive of bid
EXIT_REPRICE_WAIT_MS = 500        # wait this long before refreshing depth and repricing an unfilled exit
MAX_EXIT_REPRICE_ATTEMPTS = 4     # bounded escalation steps before falling through to the emergency exit
EXIT_RETRY_INTERVAL_MS = 500

# Priority order documented explicitly (spec #15) -- evaluated top to
# bottom every tick while a position is open; the first condition that
# fires wins, even if others would also apply this tick.
EXIT_PRIORITY_ORDER = [
    "EMERGENCY_RISK_EXIT",
    "HARD_STOP_LOSS",
    "SIGNAL_INVALIDATION",
    "PROFIT_TARGET",
    "TIME_STOP",
    "END_OF_SESSION_MANDATORY_EXIT",
]

# ---------------------------------------------------------------------
# Position sizing / capital (spec #10)
# ---------------------------------------------------------------------
FNO_CAPITAL = float(os.environ.get("FNO_TRADING_CAPITAL", "5000"))  # this strategy's OWN capital allocation,
                                                                        # distinct from the equity bot's TRADING_CAPITAL
MAX_CAPITAL_PER_TRADE_PCT = 100.0   # no single trade's premium outlay exceeds this % of FNO_CAPITAL
MAX_RISK_PER_TRADE_PCT = 5.0       # max % of FNO_CAPITAL this trade's stop-loss is allowed to risk
MAX_DAILY_LOSS = 5000.0            # absolute rupee kill-switch for the day
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_LOSSES = 2
MAX_CAPITAL_EXPOSURE_PCT = 30.0    # cap on total F&O capital deployed at once (relevant once >1 concurrent position is allowed)

# ---------------------------------------------------------------------
# Shared-capital coordination with the equity bot (see Finding 2,
# architecture review) -- both bots draw on the same broker account.
# This is additive: SHARED_CAPITAL_CHECK_ENABLED=False makes this
# bot behave exactly as if it were the only consumer of margin,
# useful for isolated testing.
# ---------------------------------------------------------------------
SHARED_CAPITAL_CHECK_ENABLED = True
SHARED_CAPITAL_LEDGER_PATH = os.environ.get(
    "FNO_SHARED_CAPITAL_LEDGER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared_capital_ledger.json"),
)

# ---------------------------------------------------------------------
# Order safety / duplicate protection (spec #28)
# ---------------------------------------------------------------------
ORDER_VERIFY_MAX_WAIT_SECONDS = 8
ORDER_VERIFY_POLL_INTERVAL_SECONDS = 0.5
MAX_ORDER_RETRIES = 3

# ---------------------------------------------------------------------
# Directional signal selection (spec #6) -- modular, shadow-testable.
# AUTHORIZED_SIGNAL is the ONLY candidate allowed to produce a live/
# paper order; every candidate in SHADOW_SIGNAL_CANDIDATES still runs
# and is logged every session, purely for comparison. None is assumed
# correct -- see strategies/signal_candidates.py.
# ---------------------------------------------------------------------
AUTHORIZED_SIGNAL = None   # None = no signal is authorized to trade yet; SHADOW-only until explicitly set
SHADOW_SIGNAL_CANDIDATES = [
    "premium_imbalance",
    "premium_rate_of_change",
    "underlying_open_vs_prev_close",
    "bid_ask_imbalance",
    "depth_imbalance",
]
SIGNAL_CONFIRMATION_WINDOW_MS = 1500  # short confirmation window some candidates use before committing

# Counterfactual capture horizons (spec #22), even when flat/no trade
COUNTERFACTUAL_HORIZONS_SECONDS = [1, 2, 5, 10, 30, 60]

# ---------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------
DEBUG_TICK_LOGGING = False   # if True, logs every tick -- noisy, debug-only per spec #30

# ---------------------------------------------------------------------
# Overrides from an (optional) F&O configuration UI, same pattern as
# the equity bot's user_config.json -- completely separate file, never
# shared with or read by the equity bot.
# ---------------------------------------------------------------------
_USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config_fno.json")

if os.path.exists(_USER_CONFIG_PATH):
    with open(_USER_CONFIG_PATH) as _f:
        _overrides = json.load(_f)

    UNDERLYING = _overrides.get("underlying", UNDERLYING)
    UNIVERSE_MODE = _overrides.get("universe_mode", UNIVERSE_MODE).upper()
    MODE = _overrides.get("mode", MODE)
    ENTRY_START_TIME = _overrides.get("entry_start_time", ENTRY_START_TIME)
    ENTRY_END_TIME = _overrides.get("entry_end_time", ENTRY_END_TIME)
    ENTRY_BUFFER_PCT = _overrides.get("entry_buffer_pct", ENTRY_BUFFER_PCT)
    MAX_ENTRY_SLIPPAGE_PCT = _overrides.get("max_entry_slippage_pct", MAX_ENTRY_SLIPPAGE_PCT)
    ENTRY_TIMEOUT_MS = _overrides.get("entry_timeout_ms", ENTRY_TIMEOUT_MS)
    MAX_ENTRY_ATTEMPTS = _overrides.get("max_entry_attempts", MAX_ENTRY_ATTEMPTS)
    TARGET_PCT = _overrides.get("target_pct", TARGET_PCT)
    STOP_LOSS_PCT = _overrides.get("stop_loss_pct", STOP_LOSS_PCT)
    MAX_HOLD_SECONDS = _overrides.get("max_hold_seconds", MAX_HOLD_SECONDS)
    MAX_TICK_AGE_MS = _overrides.get("max_tick_age_ms", MAX_TICK_AGE_MS)
    MAX_SPREAD_PCT = _overrides.get("max_spread_pct", MAX_SPREAD_PCT)
    FNO_CAPITAL = _overrides.get("fno_capital", FNO_CAPITAL)
    MAX_CAPITAL_PER_TRADE_PCT = _overrides.get("max_capital_per_trade_pct", MAX_CAPITAL_PER_TRADE_PCT)
    MAX_RISK_PER_TRADE_PCT = _overrides.get("max_risk_per_trade_pct", MAX_RISK_PER_TRADE_PCT)
    MAX_DAILY_LOSS = _overrides.get("max_daily_loss", MAX_DAILY_LOSS)
    MAX_TRADES_PER_DAY = _overrides.get("max_trades_per_day", MAX_TRADES_PER_DAY)
    MAX_CONSECUTIVE_LOSSES = _overrides.get("max_consecutive_losses", MAX_CONSECUTIVE_LOSSES)
    AUTHORIZED_SIGNAL = _overrides.get("authorized_signal", AUTHORIZED_SIGNAL)
    DEBUG_TICK_LOGGING = _overrides.get("debug_tick_logging", DEBUG_TICK_LOGGING)


def is_live_ack_present() -> bool:
    """True only if the exact required acknowledgement string is set.
    Never treat presence-of-any-value as sufficient -- must match exactly."""
    return os.environ.get(FNO_LIVE_ACK_ENV_VAR) == FNO_LIVE_ACK_REQUIRED_VALUE


def validate_mode():
    """
    Raises RuntimeError if MODE=LIVE without the explicit env
    acknowledgement. Call this once at startup before any broker
    connection is made. Never silently downgrades LIVE to PAPER/SHADOW
    -- refuses to start instead (spec #23).
    """
    if MODE not in ("SHADOW", "PAPER", "LIVE"):
        raise RuntimeError(f"Invalid FNO_MODE={MODE!r}; must be SHADOW, PAPER, or LIVE")
    if UNIVERSE_MODE not in ("SINGLE", "ALL_STOCK_OPTIONS"):
        raise RuntimeError(
            f"Invalid FNO_UNIVERSE_MODE={UNIVERSE_MODE!r}; "
            "must be SINGLE or ALL_STOCK_OPTIONS"
        )
    if MODE == "LIVE" and not is_live_ack_present():
        raise RuntimeError(
            f"REFUSING TO START LIVE TRADING: environment variable {FNO_LIVE_ACK_ENV_VAR} "
            f"must be set to exactly {FNO_LIVE_ACK_REQUIRED_VALUE!r}. "
            f"Set FNO_MODE=SHADOW or FNO_MODE=PAPER instead, or provide the acknowledgement."
        )
