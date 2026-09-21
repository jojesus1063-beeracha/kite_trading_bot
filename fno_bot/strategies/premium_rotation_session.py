"""Premium-rotation paper session, including the options-selling mode."""
from dataclasses import dataclass, field
from typing import List, Optional
from fno_bot.strategies.premium_rotation import TickSample, ConfirmationTracker, RotationParams
from fno_bot.strategies.premium_rotation_gate import evaluate_entry, EntryParams, EligibilityResult
from fno_bot.strategies.premium_rotation_exits import OpenPosition, ExitParams, evaluate_exit
from fno_bot.strategies.options_selling import decide_short_leg

@dataclass
class ClosedTrade:
    direction: str
    entry_price: float
    entry_time: float
    exit_price: float
    exit_time: float
    exit_reason: str
    quantity: int
    mfe_points: float
    mae_points: float
    side: str = "LONG"
    underlying_direction: Optional[str] = None

@dataclass
class TickRecord:
    timestamp: float
    underlying_price: float
    ce_price: float
    pe_price: float
    eligibility: Optional[EligibilityResult]
    position_open: bool
    trade_opened: Optional[dict] = None
    trade_closed: Optional[ClosedTrade] = None

class ShadowSession:
    """State machine. strategy_mode='SELL_PREMIUM' maps bullish->sell PE
    and bearish->sell CE. It remains simulation-only: LIVE is rejected."""
    def __init__(self, params_rotation, params_entry, params_exit, window_seconds=1.0,
                 confirmation_required_count=3, quantity=1, mode="SHADOW",
                 paper_slippage_pct=0.0, strategy_mode="LONG_PREMIUM"):
        if mode not in ("SHADOW", "PAPER"):
            raise ValueError("Premium Rotation supports SHADOW or PAPER only")
        if strategy_mode not in ("LONG_PREMIUM", "SELL_PREMIUM"):
            raise ValueError("strategy_mode must be LONG_PREMIUM or SELL_PREMIUM")
        if paper_slippage_pct < 0:
            raise ValueError("paper_slippage_pct must be non-negative")
        self.params_rotation = params_rotation
        self.params_entry = params_entry
        self.params_exit = params_exit
        self.window_seconds = window_seconds
        self.quantity = quantity
        self.mode = mode
        self.paper_slippage_pct = paper_slippage_pct
        self.strategy_mode = strategy_mode
        self.tracker = ConfirmationTracker(required_count=confirmation_required_count)
        self.history: List[TickSample] = []
        self.open_position: Optional[OpenPosition] = None
        self.mfe_points = 0.0
        self.mae_points = 0.0
        self.records: List[TickRecord] = []
        self.closed_trades: List[ClosedTrade] = []

    def _actual_leg(self, signal_direction: str) -> str:
        if self.strategy_mode == "SELL_PREMIUM":
            decision = decide_short_leg(signal_direction)
            if decision is None:
                raise ValueError(f"no short-leg mapping for {signal_direction}")
            return decision.option_type
        return signal_direction

    def _current_price_for_open_position(self):
        if self.open_position is None:
            raise RuntimeError("no open position")
        last = self.history[-1]
        return last.ce_price if self.open_position.direction == "CE" else last.pe_price

    def _paper_entry_price(self, market_price):
        if self.strategy_mode == "SELL_PREMIUM":
            return market_price * (1 - self.paper_slippage_pct / 100)
        return market_price * (1 + self.paper_slippage_pct / 100)

    def _paper_exit_price(self, market_price):
        if self.strategy_mode == "SELL_PREMIUM":
            return market_price * (1 + self.paper_slippage_pct / 100)
        return market_price * (1 - self.paper_slippage_pct / 100)

    def on_tick(self, tick, now_hhmm="10:00", kill_switch_allowed=True,
                kill_switch_reason="", opening_protected=False):
        self.history.append(tick)
        if self.open_position is None:
            eligibility = evaluate_entry(
                self.history, tick.timestamp, self.window_seconds, self.tracker,
                self.params_rotation, self.params_entry,
                kill_switch_allowed=kill_switch_allowed,
                kill_switch_reason=kill_switch_reason,
                opening_protected=opening_protected,
            )
            trade_opened = None
            if eligibility.eligible:
                actual_leg = self._actual_leg(eligibility.classification if self.strategy_mode == "SELL_PREMIUM" else eligibility.direction)
                market_price = tick.ce_price if actual_leg == "CE" else tick.pe_price
                entry_price = self._paper_entry_price(market_price) if self.mode == "PAPER" else market_price
                side = "SHORT" if self.strategy_mode == "SELL_PREMIUM" else "LONG"
                self.open_position = OpenPosition(
                    direction=actual_leg, entry_price=entry_price,
                    entry_time=tick.timestamp, peak_favorable_price=entry_price, side=side,
                )
                self.mfe_points = 0.0
                self.mae_points = 0.0
                trade_opened = {
                    "direction": actual_leg,
                    "underlying_direction": eligibility.classification,
                    "side": side,
                    "entry_price": entry_price,
                    "entry_time": tick.timestamp,
                }
            record = TickRecord(tick.timestamp, tick.underlying_price, tick.ce_price, tick.pe_price,
                                eligibility, self.open_position is not None, trade_opened=trade_opened)
            self.records.append(record)
            return record

        current_price = self._current_price_for_open_position()
        move = ((current_price - self.open_position.entry_price)
                if self.open_position.side == "LONG"
                else (self.open_position.entry_price - current_price))
        self.mfe_points = max(self.mfe_points, move)
        self.mae_points = min(self.mae_points, move)
        if self.open_position.side == "LONG":
            if current_price > self.open_position.peak_favorable_price:
                self.open_position.peak_favorable_price = current_price
        else:
            if current_price < self.open_position.peak_favorable_price:
                self.open_position.peak_favorable_price = current_price

        from fno_bot.strategies.premium_rotation import calculate_window_features
        features = calculate_window_features(self.history, self.window_seconds)
        exit_reason = evaluate_exit(self.open_position, current_price, features,
                                    self.params_rotation, self.params_exit,
                                    tick.timestamp, now_hhmm)
        trade_closed = None
        if exit_reason is not None:
            exit_price = self._paper_exit_price(current_price) if self.mode == "PAPER" else current_price
            trade_closed = ClosedTrade(
                direction=self.open_position.direction,
                entry_price=self.open_position.entry_price,
                entry_time=self.open_position.entry_time,
                exit_price=exit_price,
                exit_time=tick.timestamp,
                exit_reason=exit_reason,
                quantity=self.quantity,
                mfe_points=self.mfe_points,
                mae_points=self.mae_points,
                side=self.open_position.side,
            )
            self.closed_trades.append(trade_closed)
            self.open_position = None

        record = TickRecord(tick.timestamp, tick.underlying_price, tick.ce_price, tick.pe_price,
                            None, self.open_position is not None, trade_closed=trade_closed)
        self.records.append(record)
        return record

@dataclass(frozen=True)
class CounterfactualResult:
    rejection_timestamp: float
    direction: str
    reference_price: float
    horizons: dict

def compute_counterfactual(history, rejection_index, direction, horizon_seconds):
    ref = history[rejection_index]
    ref_price = ref.ce_price if direction == "CE" else ref.pe_price
    horizons_out = {}
    for h in horizon_seconds:
        future = [s for s in history[rejection_index:] if s.timestamp <= ref.timestamp + h]
        if len(future) < 2 or ref_price <= 0:
            continue
        prices = [s.ce_price if direction == "CE" else s.pe_price for s in future]
        max_fav = max(prices) - ref_price
        max_adv = min(prices) - ref_price
        horizons_out[h] = {"max_favorable_pct": round(max_fav / ref_price * 100, 3),
                           "max_adverse_pct": round(max_adv / ref_price * 100, 3)}
    return CounterfactualResult(ref.timestamp, direction, ref_price, horizons_out)
