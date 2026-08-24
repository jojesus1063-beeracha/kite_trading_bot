"""
PREMIUM_ROTATION_SHADOW -- detection core (sections 1-10 of spec).

Tick-driven, NOT candle-driven (distinct from INTRADAY_OPTIONS_V1).
Detects rapid CE/PE premium rotation confirmed by the underlying,
rather than predicting direction outright.

This module is pure logic: no broker, no WebSocket, no file I/O.
Callers feed it TickSample objects as they arrive; it never looks
ahead because it never sees anything the caller hasn't already handed
it -- same "trust the caller's ordering" contract as the candle
replay engine, just at tick granularity instead of candle granularity.

SHADOW-only by construction: nothing in this file calls or imports
order-placement functions. Section 18's isolation requirement is true
by omission, not by a runtime flag.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from collections import deque

# --- classification labels (section 8) --------------------------------
BULLISH_ROTATION = "BULLISH_ROTATION"
BEARISH_ROTATION = "BEARISH_ROTATION"
VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
VOLATILITY_CONTRACTION = "VOLATILITY_CONTRACTION"
NO_DIRECTION = "NO_DIRECTION"
CONFLICTING_SIGNAL = "CONFLICTING_SIGNAL"


@dataclass(frozen=True)
class TickSample:
    """One synchronized CE/PE/underlying observation. `timestamp` is
    monotonic seconds (caller's clock), not wall-clock -- consistent
    with tick_store.py's existing `received_monotonic` convention."""
    timestamp: float
    ce_price: float
    pe_price: float
    underlying_price: float


@dataclass(frozen=True)
class WindowFeatures:
    window_seconds: float
    elapsed_seconds: float
    ce_momentum_pct: float
    pe_momentum_pct: float
    underlying_momentum_pct: float
    difference_velocity: float   # (CE-PE) change per second
    premium_difference: float    # current CE - PE
    premium_ratio: Optional[float]
    ratio_change: Optional[float]


def _find_reference(history: List[TickSample], now_time: float, window_seconds: float) -> Optional[TickSample]:
    """The most recent sample at least `window_seconds` old. None if no
    sample is old enough yet -- caller must treat that as 'not enough
    history for this window', never extrapolate or assume zero."""
    candidates = [s for s in history if s.timestamp <= now_time - window_seconds]
    if not candidates:
        return None
    return candidates[-1]


def calculate_window_features(history: List[TickSample], window_seconds: float) -> Optional[WindowFeatures]:
    """history: all samples up to and including 'now', oldest first.
    Returns None if there isn't yet enough history to cover this
    window -- explicit unavailability, never a fabricated 0."""
    if len(history) < 2:
        return None
    current = history[-1]
    ref = _find_reference(history, current.timestamp, window_seconds)
    if ref is None:
        return None
    elapsed = current.timestamp - ref.timestamp
    if elapsed <= 0 or ref.ce_price <= 0 or ref.pe_price <= 0 or ref.underlying_price <= 0:
        return None

    ce_momentum = (current.ce_price - ref.ce_price) / ref.ce_price
    pe_momentum = (current.pe_price - ref.pe_price) / ref.pe_price
    underlying_momentum = (current.underlying_price - ref.underlying_price) / ref.underlying_price

    diff_now = current.ce_price - current.pe_price
    diff_ref = ref.ce_price - ref.pe_price
    velocity = (diff_now - diff_ref) / elapsed

    ratio_now = current.ce_price / current.pe_price if current.pe_price > 0 else None
    ratio_ref = ref.ce_price / ref.pe_price if ref.pe_price > 0 else None
    ratio_change = (ratio_now - ratio_ref) if (ratio_now is not None and ratio_ref is not None) else None

    return WindowFeatures(
        window_seconds=window_seconds, elapsed_seconds=elapsed,
        ce_momentum_pct=ce_momentum * 100, pe_momentum_pct=pe_momentum * 100,
        underlying_momentum_pct=underlying_momentum * 100,
        difference_velocity=velocity, premium_difference=diff_now,
        premium_ratio=ratio_now, ratio_change=ratio_change,
    )


@dataclass(frozen=True)
class RotationParams:
    """All experimental, all configurable -- section 3/20 explicitly
    forbids assuming these are optimal defaults."""
    ce_momentum_min_pct: float = 2.0      # min % move to count as "rising"
    pe_weakness_max_pct: float = 0.5      # PE momentum must be below this (i.e. flat or falling) to count as "weak"
    velocity_min: float = 5.0             # min premium-points/sec to count as "rapid"
    both_rising_threshold_pct: float = 3.0    # both CE and PE above this -> volatility expansion
    both_falling_threshold_pct: float = -3.0  # both below this -> volatility contraction
    underlying_confirm_min_pct: float = 0.05  # min underlying move to "confirm" a direction


def classify_rotation(features: WindowFeatures, params: RotationParams) -> str:
    """Section 8: pure premium-behavior classification, underlying NOT
    considered here -- confirmation against the underlying is a
    SEPARATE step (check_underlying_confirmation), kept apart so a
    caller can inspect 'what did the premiums do' independent of
    'did the index agree', per section 7's explicit requirement not
    to let premiums alone decide."""
    ce_rising = features.ce_momentum_pct >= params.both_rising_threshold_pct
    pe_rising = features.pe_momentum_pct >= params.both_rising_threshold_pct
    ce_falling = features.ce_momentum_pct <= params.both_falling_threshold_pct
    pe_falling = features.pe_momentum_pct <= params.both_falling_threshold_pct

    if ce_rising and pe_rising:
        return VOLATILITY_EXPANSION
    if ce_falling and pe_falling:
        return VOLATILITY_CONTRACTION

    bullish = (
        features.ce_momentum_pct >= params.ce_momentum_min_pct
        and features.pe_momentum_pct <= params.pe_weakness_max_pct
        and features.difference_velocity >= params.velocity_min
    )
    bearish = (
        features.pe_momentum_pct >= params.ce_momentum_min_pct
        and features.ce_momentum_pct <= params.pe_weakness_max_pct
        and features.difference_velocity <= -params.velocity_min
    )
    if bullish:
        return BULLISH_ROTATION
    if bearish:
        return BEARISH_ROTATION
    return NO_DIRECTION


def check_underlying_confirmation(direction: str, features: WindowFeatures, params: RotationParams) -> bool:
    """direction: BULLISH_ROTATION or BEARISH_ROTATION only."""
    if direction == BULLISH_ROTATION:
        return features.underlying_momentum_pct >= params.underlying_confirm_min_pct
    if direction == BEARISH_ROTATION:
        return features.underlying_momentum_pct <= -params.underlying_confirm_min_pct
    return False


def resolve_classification(features: WindowFeatures, params: RotationParams) -> str:
    """Combines pure premium rotation with underlying confirmation into
    the final label, adding CONFLICTING_SIGNAL when they disagree."""
    raw = classify_rotation(features, params)
    if raw in (BULLISH_ROTATION, BEARISH_ROTATION):
        if not check_underlying_confirmation(raw, features, params):
            return CONFLICTING_SIGNAL
    return raw


# --- scoring (section 9) ------------------------------------------------

def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _scale(value: float, span: float) -> float:
    """Maps value in [-span, +span] to [0, 100], clamped."""
    return _clip((value / span) * 50 + 50)


def compute_scores(features: WindowFeatures, params: RotationParams, score_span: float = 10.0) -> dict:
    """Returns {'ce_score': 0-100, 'pe_score': 0-100}. Transparent,
    additive components rather than an opaque single formula, per
    section 9's explicit request for a transparent score."""
    ce_component = _scale(features.ce_momentum_pct, score_span)
    pe_weak_component = _scale(-features.pe_momentum_pct, score_span)
    velocity_component = _scale(features.difference_velocity, score_span * 2)
    ratio_component = _scale((features.ratio_change or 0) * 1000, score_span)
    underlying_component = _scale(features.underlying_momentum_pct, 1.0)

    ce_score = (ce_component + pe_weak_component + velocity_component + ratio_component + underlying_component) / 5

    pe_component = _scale(features.pe_momentum_pct, score_span)
    ce_weak_component = _scale(-features.ce_momentum_pct, score_span)
    velocity_component_pe = _scale(-features.difference_velocity, score_span * 2)
    ratio_component_pe = _scale(-(features.ratio_change or 0) * 1000, score_span)
    underlying_component_pe = _scale(-features.underlying_momentum_pct, 1.0)

    pe_score = (pe_component + ce_weak_component + velocity_component_pe + ratio_component_pe + underlying_component_pe) / 5

    return {"ce_score": round(ce_score, 2), "pe_score": round(pe_score, 2)}


# --- confirmation / persistence (section 5-6) ---------------------------

class ConfirmationTracker:
    """Requires a classification to persist for N consecutive
    evaluations (or a minimum duration) before it's treated as
    eligible -- prevents a single abnormal tick from creating a
    trade, per spec's explicit requirement."""
    def __init__(self, required_count: int = 3, required_seconds: float = 0.0):
        self.required_count = required_count
        self.required_seconds = required_seconds
        self._current_label: Optional[str] = None
        self._streak_count = 0
        self._streak_start_time: Optional[float] = None

    def update(self, label: str, now_time: float) -> bool:
        """Returns True if `label` has now persisted long enough to be
        confirmed. Any label change resets the streak."""
        if label != self._current_label:
            self._current_label = label
            self._streak_count = 1
            self._streak_start_time = now_time
        else:
            self._streak_count += 1
        duration = now_time - self._streak_start_time if self._streak_start_time is not None else 0.0
        return self._streak_count >= self.required_count and duration >= self.required_seconds


# --- anti-chase (section 10) ---------------------------------------------

def check_not_extended(history: List[TickSample], direction: str, lookback_seconds: float,
                        max_extension_pct: float) -> Optional[str]:
    """Returns a rejection reason string if the premium has already
    moved too far from its recent local low/high, else None (not
    rejected). direction: 'CE' or 'PE'."""
    if len(history) < 2:
        return None
    now_time = history[-1].timestamp
    window = [s for s in history if s.timestamp >= now_time - lookback_seconds]
    if len(window) < 2:
        return None
    current_price = history[-1].ce_price if direction == "CE" else history[-1].pe_price
    prices_in_window = [s.ce_price if direction == "CE" else s.pe_price for s in window]
    local_low = min(prices_in_window)
    if local_low <= 0:
        return None
    extension_pct = (current_price - local_low) / local_low * 100
    if extension_pct > max_extension_pct:
        return f"ENTRY_TOO_EXTENDED: {extension_pct:.2f}% above local low, exceeds {max_extension_pct}%"
    return None
