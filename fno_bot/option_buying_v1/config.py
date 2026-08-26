"""Dedicated configuration for the F&O option-buying v1 path.

These names are deliberately not aliases of the equity bot's settings.
PAPER is the only executable mode in v1; live order placement fails closed.
"""
import os
from dataclasses import dataclass


FNO_ENABLED = True
FNO_PAPER_TRADING = True
FNO_CAPITAL = 5_000.0
FNO_EXCHANGE = "NFO"
FNO_OPTION_BUY_ONLY = True
FNO_STRIKE_MODE = "ATM"
FNO_EXPIRY_MODE = "NEAREST_ELIGIBLE"
FNO_BLOCK_SAME_DAY_EXPIRY = True
FNO_MIN_DTE = 1
FNO_ENTRY_START = "09:27"
FNO_ENTRY_END = "09:59"
FNO_FORCE_SQUARE_OFF_TIME = "15:10"
FNO_MAX_TRADES_PER_DAY = 3
FNO_MAX_OPEN_POSITIONS = 2
FNO_LOTS_PER_TRADE = 1
FNO_REQUIRE_WHOLE_LOTS = True
FNO_SKIP_UNAFFORDABLE_ATM = True
FNO_ESTIMATED_SLIPPAGE_PCT = 0.20
FNO_RESERVE_ESTIMATED_CHARGES = True
FNO_ESTIMATED_CHARGES_PCT = 0.20
FNO_MIN_ESTIMATED_CHARGES = 25.0
FNO_REQUIRE_CURRENT_INSTRUMENT_MASTER = True
FNO_EXIT_POLICY = "MEASURE_ONLY"
FNO_TRADE_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fno_option_buying_v1_trades.jsonl",
)
FNO_V1_SIGNAL_LOG_DIR = os.environ.get(
    "FNO_V1_SIGNAL_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "signal_logs"),
)
FNO_V1_ACCESS_TOKEN_FILE = os.environ.get(
    "FNO_V1_ACCESS_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "access_token.txt"),
)
FNO_V1_STATE_PATH = os.environ.get(
    "FNO_V1_STATE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_option_buying_v1_state.json"),
)
FNO_V1_STATUS_PATH = os.environ.get(
    "FNO_V1_STATUS_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fno_option_buying_v1_status.json"),
)
FNO_V1_POLL_SECONDS = 1.0
FNO_V1_MAX_TICK_AGE_MS = 5_000.0
FNO_V1_MAX_SIGNAL_AGE_SECONDS = 30.0
FNO_V1_FORCE_EXIT_RETRY_UNTIL = "15:15"
FNO_V1_REQUIRE_EXECUTED_EQUITY_SIGNAL = True


@dataclass(frozen=True)
class OptionBuyingConfig:
    enabled: bool = FNO_ENABLED
    paper_trading: bool = FNO_PAPER_TRADING
    capital: float = FNO_CAPITAL
    exchange: str = FNO_EXCHANGE
    option_buy_only: bool = FNO_OPTION_BUY_ONLY
    strike_mode: str = FNO_STRIKE_MODE
    expiry_mode: str = FNO_EXPIRY_MODE
    block_same_day_expiry: bool = FNO_BLOCK_SAME_DAY_EXPIRY
    min_dte: int = FNO_MIN_DTE
    entry_start: str = FNO_ENTRY_START
    entry_end: str = FNO_ENTRY_END
    force_square_off_time: str = FNO_FORCE_SQUARE_OFF_TIME
    max_trades_per_day: int = FNO_MAX_TRADES_PER_DAY
    max_open_positions: int = FNO_MAX_OPEN_POSITIONS
    lots_per_trade: int = FNO_LOTS_PER_TRADE
    require_whole_lots: bool = FNO_REQUIRE_WHOLE_LOTS
    skip_unaffordable_atm: bool = FNO_SKIP_UNAFFORDABLE_ATM
    estimated_slippage_pct: float = FNO_ESTIMATED_SLIPPAGE_PCT
    reserve_estimated_charges: bool = FNO_RESERVE_ESTIMATED_CHARGES
    estimated_charges_pct: float = FNO_ESTIMATED_CHARGES_PCT
    min_estimated_charges: float = FNO_MIN_ESTIMATED_CHARGES
    require_current_instrument_master: bool = FNO_REQUIRE_CURRENT_INSTRUMENT_MASTER
    exit_policy: str = FNO_EXIT_POLICY
    trade_log_path: str = FNO_TRADE_LOG_PATH
    signal_log_dir: str = FNO_V1_SIGNAL_LOG_DIR
    access_token_file: str = FNO_V1_ACCESS_TOKEN_FILE
    state_path: str = FNO_V1_STATE_PATH
    status_path: str = FNO_V1_STATUS_PATH
    poll_seconds: float = FNO_V1_POLL_SECONDS
    max_tick_age_ms: float = FNO_V1_MAX_TICK_AGE_MS
    max_signal_age_seconds: float = FNO_V1_MAX_SIGNAL_AGE_SECONDS
    force_exit_retry_until: str = FNO_V1_FORCE_EXIT_RETRY_UNTIL
    require_executed_equity_signal: bool = FNO_V1_REQUIRE_EXECUTED_EQUITY_SIGNAL

    def validate(self) -> None:
        if not self.enabled:
            raise RuntimeError("F&O option buying is disabled")
        if not self.paper_trading:
            raise RuntimeError("F&O option-buying v1 is PAPER-only; live orders are blocked")
        if self.exchange != "NFO":
            raise ValueError("F&O option-buying v1 supports NFO only")
        if not self.option_buy_only:
            raise ValueError("F&O option-buying v1 cannot sell options short")
        if self.capital <= 0 or self.min_dte < 1:
            raise ValueError("F&O capital must be positive and minimum DTE must be at least one")
        if self.lots_per_trade != 1:
            raise ValueError("v1 deliberately permits exactly one whole lot per entry")
        if self.strike_mode != "ATM" or self.expiry_mode != "NEAREST_ELIGIBLE":
            raise ValueError("v1 permits ATM and NEAREST_ELIGIBLE resolution only")
