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
ENTRY_TIMEFRAME = "3minute"
ENTRY_HISTORY_LOOKBACK_DAYS = 5

# ---------------------------------------------------------------------
# Indicator settings
# ---------------------------------------------------------------------
TREND_EMA_FAST = 9      # on 15-min chart
TREND_EMA_SLOW = 35     # on 15-min chart
ENTRY_EMA = 20          # on entry-timeframe chart
VOLUME_LOOKBACK = 20    # bars, for entry-candle average-volume comparison
VOLUME_MULTIPLIER = 1.2  # entry candle volume must exceed avg volume * this

# Paper-only experiment policy. "observe" calculates the original strict
# gate but lets a simpler baseline continue so rejected cases can acquire
# paper-trade outcomes. Production behavior remains "enforce" by default.
TREND_GATE_MODE = "enforce"
PULLBACK_GATE_MODE = "enforce"
EXPERIMENTAL_PAPER_ONLY = True
EXPERIMENT_OBSERVATION_FILE = "runtime/strategy_experiment.jsonl"

# ADX (Average Directional Index) — measures trend STRENGTH (0-100),
# used as an optional filter on top of the existing EMA/VWAP trend
# check. When enabled, a 15-min candle only counts as trending if ADX
# is above the threshold — filters out choppy periods where price
# happens to sit above/below the EMAs without a real trend behind it.
# Toggle this to False to compare strategy performance with/without.
USE_ADX_FILTER = False
ADX_PERIOD = 14
ADX_THRESHOLD = 25  # Wilder's original suggested threshold for "trending"

# Additive, opt-in alternative to the binary USE_ADX_FILTER above.
# "" (empty/unset) -> mirrors USE_ADX_FILTER exactly (off or binary).
# "off"     -> no ADX gating at all, regardless of USE_ADX_FILTER
# "binary"  -> same pass/fail behavior as USE_ADX_FILTER=True
# "dynamic" -> tiered confidence (REJECTED/MEDIUM/HIGH/VERY_STRONG),
#              all tiers except REJECTED allow the trade
# Does NOT change position sizing or existing BUY/SELL logic either way.
ADX_MODE = ""
ADX_DYNAMIC_MIN = 20     # below this -> REJECTED
# ADX_THRESHOLD above doubles as the MEDIUM/HIGH boundary in dynamic mode
ADX_DYNAMIC_STRONG = 35  # at/above this -> VERY_STRONG

# ---------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------
RISK_REWARD_MIN = 2.0          # minimum reward:risk ratio (1:2)
RISK_PER_TRADE_PCT = 1.0       # % of capital risked per trade
MAX_TRADES_PER_DAY = 100
MAX_OPEN_POSITIONS = 10          # cap on simultaneous different-symbol positions
MAX_POSITION_SIZE_PCT = 20.0     # no single position's notional value (qty * entry) exceeds this % of CAPITAL
CHECK_MARGIN_BEFORE_ENTRY = True # verify real Zerodha margin via order_margins() before placing a live entry order
MAX_DAILY_LOSS_PCT = 3.0       # kill-switch: stop trading if daily loss exceeds this %
CAPITAL = float(os.environ.get("TRADING_CAPITAL", "5000"))  # your intraday capital, INR

# Stop-loss is placed at the low (long) / high (short) of the signal
# candle, minus/plus a small buffer to avoid getting stopped out by
# noise.
SL_BUFFER_PCT = 0.05  # 0.05% buffer beyond the signal candle's extreme
SL_BUFFER_PCT_SELL = None  # if set, overrides SL_BUFFER_PCT for SELL trades only.
                            # None (default) = use SL_BUFFER_PCT for both directions,
                            # fully backward compatible. Set wider for SELL to account
                            # for sharper short-squeeze risk vs typical long drawdowns.

CIRCUIT_PROXIMITY_PCT = 2.0  # block entries within this % of the relevant
                              # circuit limit (upper for BUY, lower for SELL) --
                              # avoids trades that could get trapped by a
                              # locked circuit with no exit liquidity

ENABLE_MARKET_ALIGNMENT_FILTER = False  # block entries whose market_alignment is
                                          # MISALIGNED or STRONG_MISALIGNMENT.
                                          # Default off -- enable explicitly once ready.

MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY", "")  # required for the news filter
ENABLE_NEWS_FILTER = False  # additional risk layer, purely additive -- never generates
                             # BUY/SELL signals, only evaluates whether an existing
                             # technical signal should proceed (see news_filter.py)
NEWS_LOOKBACK_HOURS = 24
NEWS_CACHE_MINUTES = 5
NEWS_TIMEOUT_SECONDS = 2
NEGATIVE_NEWS_BLOCK = True
POSITIVE_NEWS_CONFIDENCE_BONUS = 5
NEGATIVE_NEWS_CONFIDENCE_PENALTY = 25

ENABLE_PRICE_ACTION = False  # additional confirmation layer, pure confidence
                              # modifier -- never independently rejects a trade
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

STOP_LOSS_PERCENT = 0.45  # fixed stop measured from confirmed fill
PROFIT_TARGET_PERCENT = 0.70  # fixed profit target, replaces trailing-stop-based
                                # exits entirely when ENABLE_FIXED_TARGET is True
ENABLE_FIXED_TARGET = True
ENABLE_TRAILING_STOP = False  # disabled when fixed-target mode is on -- the whole
                                # point is booking quick, consistent profits rather
                                # than letting winners run
EXIT_IMMEDIATELY_AT_TARGET = True

# Hybrid paper/live exit. One entry keeps a single total risk budget; half
# exits at 1R and the remainder targets 2R. After the confirmed scalp fill,
# paper mode moves its local stop to entry and live mode atomically replaces
# the old full-size broker stop with a verified break-even runner stop.
ENABLE_HYBRID_EXIT = True
HYBRID_SCALP_FRACTION = 0.50
HYBRID_SCALP_R = 1.0
HYBRID_RUNNER_R = 2.0
HYBRID_MOVE_STOP_TO_BREAKEVEN = True

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

# Square-off: start two minutes before Zerodha's earliest 15:10
# intraday cutoff. The buffer lets protective-stop cancellation and
# market-exit submission finish before new MIS orders are rejected.
FORCE_SQUARE_OFF_TIME = "15:08"

# Trading window — don't take new entries in the first/last few minutes
# of the session (high volatility / low liquidity for stops).
NO_ENTRY_BEFORE = "09:25"
NO_ENTRY_AFTER = "15:00"

# Live vs paper mode. ALWAYS start with PAPER_TRADING = True.
PAPER_TRADING = True

# ---------------------------------------------------------------------
# Candle-aligned scheduler (opt-in infrastructure change)
# ---------------------------------------------------------------------
# When False (default): main.py runs its original loop exactly as
# before -- full watchlist scan + position checks together, every
# POLL_SECONDS. Nothing about this section has any effect.
#
# When True: main.py uses scheduler.py instead. Full scans (fetch +
# indicators + signal evaluation, unchanged) only run once per newly
# completed ENTRY_TIMEFRAME candle. Between scans, only open positions
# are checked (stop-loss/target), on a much shorter interval, without
# fetching/evaluating the rest of the watchlist. Trading logic itself
# is byte-for-byte identical either way.
ENABLE_CANDLE_ALIGNED_POLLING = True
POSITION_CHECK_SECONDS = 25   # how often to check open positions between scans
CANDLE_COMPLETION_BUFFER_SECONDS = 10
SCAN_BUFFER_SECONDS = 12      # broker finalisation buffer + 2s safety margin
ENTRY_SCAN_SHORTLIST_SIZE = 60  # top daily auto-watchlist priorities

# Sanity-check thresholds -- purely observational, log-only. Never skip
# or alter any trading action based on these; they just surface timing
# problems (slow network, API degradation, an overloaded watchlist)
# early via WARNING/CRITICAL log lines instead of requiring someone to
# notice by eye. Only meaningful when ENABLE_CANDLE_ALIGNED_POLLING=True.
SCHEDULER_WARNING_SCAN_SECONDS = 90     # full scan taking longer than this -> WARNING
SCHEDULER_CRITICAL_SCAN_SECONDS = 120   # full scan taking longer than this -> CRITICAL
POSITION_CHECK_WARNING_SECONDS = 40     # one position-check pass taking longer than this -> WARNING
POSITION_CHECK_CRITICAL_SECONDS = 60    # one position-check pass taking longer than this -> CRITICAL
SCAN_DELAY_WARNING_SECONDS = 30         # scan starting this late vs its target time -> WARNING
SCAN_DELAY_CRITICAL_SECONDS = 60        # scan starting this late vs its target time -> CRITICAL

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
    STOP_LOSS_PERCENT = float(_overrides.get("sl_buffer_pct", _overrides.get("stop_loss_percent", STOP_LOSS_PERCENT)))
    NO_ENTRY_BEFORE = _overrides.get("no_entry_before", NO_ENTRY_BEFORE)
    NO_ENTRY_AFTER = _overrides.get("no_entry_after", NO_ENTRY_AFTER)
    ENABLE_FIXED_TARGET = _overrides.get("enable_fixed_target", ENABLE_FIXED_TARGET)
    ENABLE_TRAILING_STOP = _overrides.get("enable_trailing_stop", ENABLE_TRAILING_STOP)
    EXIT_IMMEDIATELY_AT_TARGET = _overrides.get("exit_immediately_at_target", EXIT_IMMEDIATELY_AT_TARGET)
    ENABLE_HYBRID_EXIT = _overrides.get("enable_hybrid_exit", ENABLE_HYBRID_EXIT)
    HYBRID_SCALP_FRACTION = float(_overrides.get("hybrid_scalp_fraction", HYBRID_SCALP_FRACTION))
    HYBRID_SCALP_R = float(_overrides.get("hybrid_scalp_r", HYBRID_SCALP_R))
    HYBRID_RUNNER_R = float(_overrides.get("hybrid_runner_r", HYBRID_RUNNER_R))
    HYBRID_MOVE_STOP_TO_BREAKEVEN = _overrides.get("hybrid_move_stop_to_breakeven", HYBRID_MOVE_STOP_TO_BREAKEVEN)
    MAX_TRADES_PER_DAY = _overrides.get("max_trades_per_day", MAX_TRADES_PER_DAY)
    MAX_OPEN_POSITIONS = _overrides.get("max_open_positions", MAX_OPEN_POSITIONS)
    MAX_POSITION_SIZE_PCT = _overrides.get("max_position_size_pct", MAX_POSITION_SIZE_PCT)
    CHECK_MARGIN_BEFORE_ENTRY = _overrides.get("check_margin_before_entry", CHECK_MARGIN_BEFORE_ENTRY)
    MAX_DAILY_LOSS_PCT = _overrides.get("max_daily_loss_pct", MAX_DAILY_LOSS_PCT)
    TREND_EMA_FAST = _overrides.get("trend_ema_fast", TREND_EMA_FAST)
    TREND_EMA_SLOW = _overrides.get("trend_ema_slow", TREND_EMA_SLOW)
    ENTRY_EMA = _overrides.get("entry_ema", ENTRY_EMA)
    TREND_GATE_MODE = _overrides.get("trend_gate_mode", TREND_GATE_MODE)
    PULLBACK_GATE_MODE = _overrides.get("pullback_gate_mode", PULLBACK_GATE_MODE)
    EXPERIMENTAL_PAPER_ONLY = _overrides.get(
        "experimental_paper_only", EXPERIMENTAL_PAPER_ONLY
    )
    EXPERIMENT_OBSERVATION_FILE = _overrides.get(
        "experiment_observation_file", EXPERIMENT_OBSERVATION_FILE
    )
    PAPER_TRADING = _overrides.get("paper_trading", PAPER_TRADING)
    USE_ADX_FILTER = _overrides.get("use_adx_filter", USE_ADX_FILTER)
    ADX_THRESHOLD = _overrides.get("adx_threshold", ADX_THRESHOLD)
    ADX_MODE = _overrides.get("adx_mode", ADX_MODE)
    ADX_DYNAMIC_MIN = _overrides.get("adx_dynamic_min", ADX_DYNAMIC_MIN)
    ADX_DYNAMIC_STRONG = _overrides.get("adx_dynamic_strong", ADX_DYNAMIC_STRONG)
    ENABLE_CANDLE_ALIGNED_POLLING = _overrides.get("enable_candle_aligned_polling", ENABLE_CANDLE_ALIGNED_POLLING)
    POSITION_CHECK_SECONDS = _overrides.get("position_check_seconds", POSITION_CHECK_SECONDS)
    ENTRY_SCAN_SHORTLIST_SIZE = int(_overrides.get("entry_scan_shortlist_size", ENTRY_SCAN_SHORTLIST_SIZE))
    SCAN_BUFFER_SECONDS = max(
        int(_overrides.get("scan_buffer_seconds", SCAN_BUFFER_SECONDS)),
        CANDLE_COMPLETION_BUFFER_SECONDS + 2,
    )
    SCHEDULER_WARNING_SCAN_SECONDS = _overrides.get("scheduler_warning_scan_seconds", SCHEDULER_WARNING_SCAN_SECONDS)
    SCHEDULER_CRITICAL_SCAN_SECONDS = _overrides.get("scheduler_critical_scan_seconds", SCHEDULER_CRITICAL_SCAN_SECONDS)
    POSITION_CHECK_WARNING_SECONDS = _overrides.get("position_check_warning_seconds", POSITION_CHECK_WARNING_SECONDS)
    POSITION_CHECK_CRITICAL_SECONDS = _overrides.get("position_check_critical_seconds", POSITION_CHECK_CRITICAL_SECONDS)
    SCAN_DELAY_WARNING_SECONDS = _overrides.get("scan_delay_warning_seconds", SCAN_DELAY_WARNING_SECONDS)
    SCAN_DELAY_CRITICAL_SECONDS = _overrides.get("scan_delay_critical_seconds", SCAN_DELAY_CRITICAL_SECONDS)

# ---------------------------------------------------------------------
# WebSocket candle engine (opt-in infrastructure change)
# ---------------------------------------------------------------------
ENABLE_WS_CANDLES = True
# REST remains authoritative while WS-vs-REST shadow comparisons show
# material OHLC/volume differences. Promote back to "live" only after a
# reviewed session demonstrates tolerance compliance.
WS_CANDLE_MODE = "shadow"  # "shadow" or "live" -- ignored while ENABLE_WS_CANDLES is False
WS_SECTOR_INDICES = []
WS_INDICATOR_SHADOW_INTERVAL_MINUTES = 30
WS_STALE_TICK_SECONDS = 5.0
WS_ENTRY_TICK_MAX_AGE_SECONDS = 2.0
WS_MAX_SPREAD_PCT = 0.5
WS_MAX_SLIPPAGE_PCT = 0.15
MAX_ADVERSE_MOVE_PCT = None
MAX_ABSOLUTE_DRIFT_PCT = None

# ---------------------------------------------------------------------
# Higher-timeframe 200 EMA trend confirmation filter (opt-in)
# ---------------------------------------------------------------------
ENABLE_200_EMA_FILTER = True
EMA200_TIMEFRAME = "15minute"
EMA200_PERIOD = 200
EMA200_LOOKBACK = 250
EMA200_HISTORY_LOOKBACK_DAYS = 20
EMA200_ALLOW_TOUCH = False
EMA200_MIN_DISTANCE_PCT = 0.10
EMA200_SLOPE_LOOKBACK = 5

# ---------------------------------------------------------------------
# EMA200 directional signal eligibility gate (opt-in, distinct from
# ENABLE_200_EMA_FILTER above). This gate runs after evaluate() creates
# a candidate signal and rejects directions that conflict with the
# stock's EMA200 classification. ENABLE_200_EMA_FILTER is the stricter
# slope-and-distance-aware confirmation. Both can run together.
# ---------------------------------------------------------------------
ENABLE_EMA200_WATCHLIST = True
ENABLE_RVOL_FILTER = True
RVOL_LOOKBACK = 20
RVOL_THRESHOLD = 1.5

# ---------------------------------------------------------------------
# Version D -- Entry Timing Layer (entry_timing.py)
#
# Runs LAST, after every existing gate (pullback geometry, macro
# authorization, VWAP acceptance, EMA200, RVOL). Never substitutes for
# or weakens any of them.
#
# Anti-chase is ATR-NORMALIZED rather than a fixed percentage, so the
# rule behaves consistently across a Rs.100 stock and a Rs.2000 one.
# ATR is computed locally inside entry_timing.py from df_5m's OHLC --
# add_indicators() does NOT attach an "atr" column to df_5m (verified).
#
# ENABLE_VOLUME_ACCELERATION_FILTER defaults False deliberately: its
# effect must be MEASURED in replay before it is made mandatory. When
# a filter is off, its metric is still computed and logged, so replay
# can quantify exactly what enabling it would have cost or gained.
# ---------------------------------------------------------------------
ENABLE_ENTRY_TIMING_FILTER = True
MAX_ENTRY_EXTENSION_ATR = 1.50
ENABLE_CONFIRMATION_QUALITY_FILTER = True
MIN_CONFIRMATION_BODY_RATIO = 0.50
ENABLE_VOLUME_ACCELERATION_FILTER = True
MIN_CONFIRMATION_VOLUME_ACCELERATION = 1.10
