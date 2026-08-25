"""
PREMIUM_ROTATION_SHADOW -- session orchestrator (ties sections 1-13
and 17 together) and counterfactual tracking.

SHADOW-only: `PaperPosition` is a pure in-memory record, never a real
order. This module has no import of anything under fno_bot.broker or
fno_bot.execution -- opening/closing a "position" here means updating
a dataclass, nothing else.

Every tick produces a TickRecord regardless of whether anything
happened -- flat-and-nothing-eligible is exactly as important to log
as a real trade (section 16's explicit requirement), so callers must
persist every TickRecord this module emits, not just the ones with
a trade.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from fno_bot.strategies.premium_rotation import TickSample, ConfirmationTracker, RotationParams
from fno_bot.strategies.premium_rotation_gate import evaluate_entry, EntryParams, EligibilityResult
from fno_bot.strategies.premium_rotation_exits import (
    OpenPosition, ExitParams, evaluate_exit,
)


@dataclass
class ClosedTrade:
    direction: str
    entry_price: float
    entry_time: float
    exit_price: float
    exit_time: float
    exit_reason: str
    quantity: int
    mfe_points: float   # max favorable excursion actually seen while open
    mae_points: float   # max adverse excursion actually seen while open


@dataclass
class TickRecord:
    """One row of the mandatory section-16 log. `trade_opened` /
    `trade_closed` are None on most ticks -- that's expected, not
    missing data."""
    timestamp: float
    underlying_price: float
    ce_price: float
    pe_price: float
    eligibility: Optional[EligibilityResult]
    position_open: bool
    trade_opened: Optional[dict] = None
    trade_closed: Optional[ClosedTrade] = None


class ShadowSession:
    """One continuous session's state machine. Feed ticks one at a
    time via on_tick(); it never looks ahead because it only ever
    sees what's already been fed to it."""

    def __init__(
        self, params_rotation: RotationParams, params_entry: EntryParams, params_exit: ExitParams,
        window_seconds: float = 1.0, confirmation_required_count: int = 3, quantity: int = 1,
        mode: str = "SHADOW", paper_slippage_pct: float = 0.0,
    ):
        if mode not in ("SHADOW", "PAPER"):
            raise ValueError("Premium Rotation supports SHADOW or PAPER only")
        if paper_slippage_pct < 0:
            raise ValueError("paper_slippage_pct must be non-negative")
        self.params_rotation = params_rotation
        self.params_entry = params_entry
        self.params_exit = params_exit
        self.window_seconds = window_seconds
        self.quantity = quantity
        self.mode = mode
        self.paper_slippage_pct = paper_slippage_pct
        self.tracker = ConfirmationTracker(required_count=confirmation_required_count)
        self.history: List[TickSample] = []
        self.open_position: Optional[OpenPosition] = None
        self.mfe_points = 0.0
        self.mae_points = 0.0
        self.records: List[TickRecord] = []
        self.closed_trades: List[ClosedTrade] = []

    def _current_price_for_open_position(self) -> float:
        if self.open_position is None:
            raise RuntimeError("no open position")
        last = self.history[-1]
        return last.ce_price if self.open_position.direction == "CE" else last.pe_price

    def on_tick(
        self, tick: TickSample, now_hhmm: str = "10:00",
        kill_switch_allowed: bool = True, kill_switch_reason: str = "",
        opening_protected: bool = False,
    ) -> TickRecord:
        """kill_switch_allowed / opening_protected: passed straight
        through to evaluate_entry(), which structurally cannot return
        eligible=True when either blocks it. This is the full,
        end-to-end closure of the gap originally flagged in
        premium_rotation_launcher.py's first draft -- the caller
        (launcher) now has no path to accidentally open a trade the
        gates were supposed to prevent."""
        self.history.append(tick)

        if self.open_position is None:
            eligibility = evaluate_entry(
                self.history, tick.timestamp, self.window_seconds, self.tracker,
                self.params_rotation, self.params_entry,
                kill_switch_allowed=kill_switch_allowed, kill_switch_reason=kill_switch_reason,
                opening_protected=opening_protected,
            )
            trade_opened = None
            if eligibility.eligible:
                market_price = tick.ce_price if eligibility.direction == "CE" else tick.pe_price
                entry_price = market_price
                if self.mode == "PAPER":
                    entry_price *= 1 + self.paper_slippage_pct / 100
                self.open_position = OpenPosition(
                    direction=eligibility.direction, entry_price=entry_price,
                    entry_time=tick.timestamp, peak_favorable_price=entry_price,
                )
                self.mfe_points = 0.0
                self.mae_points = 0.0
                trade_opened = {"direction": eligibility.direction, "entry_price": entry_price, "entry_time": tick.timestamp}
            record = TickRecord(tick.timestamp, tick.underlying_price, tick.ce_price, tick.pe_price,
                                 eligibility, position_open=self.open_position is not None, trade_opened=trade_opened)
            self.records.append(record)
            return record

        current_price = self._current_price_for_open_position()
        move = current_price - self.open_position.entry_price
        self.mfe_points = max(self.mfe_points, move)
        self.mae_points = min(self.mae_points, move)
        if current_price > self.open_position.peak_favorable_price:
            self.open_position.peak_favorable_price = current_price   # monotonic, never moves backward

        from fno_bot.strategies.premium_rotation import calculate_window_features
        features = calculate_window_features(self.history, self.window_seconds)

        exit_reason = evaluate_exit(
            self.open_position, current_price, features, self.params_rotation, self.params_exit,
            tick.timestamp, now_hhmm,
        )
        trade_closed = None
        if exit_reason is not None:
            exit_price = current_price
            if self.mode == "PAPER":
                exit_price *= 1 - self.paper_slippage_pct / 100
            trade_closed = ClosedTrade(
                direction=self.open_position.direction, entry_price=self.open_position.entry_price,
                entry_time=self.open_position.entry_time, exit_price=exit_price, exit_time=tick.timestamp,
                exit_reason=exit_reason, quantity=self.quantity,
                mfe_points=self.mfe_points, mae_points=self.mae_points,
            )
            self.closed_trades.append(trade_closed)
            self.open_position = None

        record = TickRecord(tick.timestamp, tick.underlying_price, tick.ce_price, tick.pe_price,
                             eligibility=None, position_open=self.open_position is not None, trade_closed=trade_closed)
        self.records.append(record)
        return record


# --- counterfactual tracking (section 17) --------------------------------

@dataclass(frozen=True)
class CounterfactualResult:
    rejection_timestamp: float
    direction: str
    reference_price: float
    horizons: dict   # {seconds: {"max_favorable_pct": x, "max_adverse_pct": y}}


def compute_counterfactual(
    history: List[TickSample], rejection_index: int, direction: str,
    horizon_seconds: List[float],
) -> CounterfactualResult:
    """For a rejected candidate at history[rejection_index], look
    FORWARD through the already-collected history (this is legitimate
    here -- unlike the live entry decision, counterfactual analysis is
    explicitly retrospective by design, computed after a session ends
    or on a rolling delayed basis, never used to inform a live trade
    decision in the same pass) and compute MFE/MAE at each horizon."""
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
        horizons_out[h] = {
            "max_favorable_pct": round(max_fav / ref_price * 100, 3),
            "max_adverse_pct": round(max_adv / ref_price * 100, 3),
        }
    return CounterfactualResult(ref.timestamp, direction, ref_price, horizons_out)
