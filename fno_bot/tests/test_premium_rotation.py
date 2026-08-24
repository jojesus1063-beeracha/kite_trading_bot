"""
Tests for premium_rotation.py -- the PREMIUM_ROTATION_SHADOW detection
core. Includes the exact deterministic scenarios from spec section 24.
"""
from fno_bot.strategies.premium_rotation import (
    TickSample, calculate_window_features, classify_rotation, resolve_classification,
    check_underlying_confirmation, compute_scores, ConfirmationTracker, check_not_extended,
    RotationParams, BULLISH_ROTATION, BEARISH_ROTATION, VOLATILITY_EXPANSION,
    VOLATILITY_CONTRACTION, NO_DIRECTION, CONFLICTING_SIGNAL,
)


def _run_scenario(ce_seq, pe_seq, underlying_seq, params, window=1.0):
    history = []
    last_resolved = None
    for i in range(len(ce_seq)):
        t = float(i)
        history.append(TickSample(t, ce_seq[i], pe_seq[i], underlying_seq[i]))
        if len(history) < 2:
            continue
        feats = calculate_window_features(history, window)
        if feats is None:
            continue
        last_resolved = resolve_classification(feats, params)
    return last_resolved


def test_insufficient_history_returns_none():
    history = [TickSample(0.0, 100, 200, 20000)]
    assert calculate_window_features(history, window_seconds=1.0) is None


def test_bullish_rotation_spec_example():
    """Exact scenario from spec section 24."""
    params = RotationParams(underlying_confirm_min_pct=0.01)
    result = _run_scenario(
        [100, 105, 112, 122, 135], [225, 220, 211, 200, 188],
        [20000, 20005, 20012, 20022, 20035], params,
    )
    assert result == BULLISH_ROTATION


def test_bearish_rotation_exact_inverse():
    params = RotationParams(underlying_confirm_min_pct=0.01)
    result = _run_scenario(
        [225, 220, 211, 200, 188], [100, 105, 112, 122, 135],
        [20000, 19995, 19988, 19978, 19965], params,
    )
    assert result == BEARISH_ROTATION


def test_volatility_expansion_both_rising():
    params = RotationParams()
    result = _run_scenario(
        [100, 108, 118, 130, 145], [150, 158, 168, 180, 195],
        [20000, 20001, 20001, 20002, 20002], params,
    )
    assert result == VOLATILITY_EXPANSION


def test_volatility_contraction_both_falling():
    params = RotationParams()
    result = _run_scenario(
        [145, 130, 118, 108, 100], [195, 180, 168, 158, 150],
        [20000, 20001, 20001, 20002, 20002], params,
    )
    assert result == VOLATILITY_CONTRACTION


def test_no_direction_flat_market():
    params = RotationParams()
    result = _run_scenario(
        [100, 100.5, 99.8, 100.2, 100.1], [150, 150.3, 149.9, 150.1, 150.0],
        [20000, 20000.5, 19999.8, 20000.2, 20000.1], params,
    )
    assert result == NO_DIRECTION


def test_conflicting_signal_when_underlying_disagrees():
    """Premiums rotate bullish but the underlying is flat/falling --
    must be CONFLICTING_SIGNAL, never silently treated as bullish."""
    params = RotationParams(underlying_confirm_min_pct=0.5)  # deliberately strict
    history = [
        TickSample(0.0, 100, 225, 20000),
        TickSample(1.0, 105, 220, 20000.5),  # underlying barely moves
    ]
    feats = calculate_window_features(history, window_seconds=1.0)
    assert feats is not None
    raw = classify_rotation(feats, params)
    assert raw == BULLISH_ROTATION   # premiums alone say bullish
    resolved = resolve_classification(feats, params)
    assert resolved == CONFLICTING_SIGNAL   # but underlying didn't confirm it


def test_confirmation_tracker_requires_persistence():
    tracker = ConfirmationTracker(required_count=3)
    assert tracker.update(BULLISH_ROTATION, 0.0) is False
    assert tracker.update(BULLISH_ROTATION, 1.0) is False
    assert tracker.update(BULLISH_ROTATION, 2.0) is True   # third consecutive match


def test_confirmation_tracker_resets_on_change():
    tracker = ConfirmationTracker(required_count=3)
    tracker.update(BULLISH_ROTATION, 0.0)
    tracker.update(BULLISH_ROTATION, 1.0)
    result = tracker.update(NO_DIRECTION, 2.0)   # streak broken
    assert result is False
    result2 = tracker.update(NO_DIRECTION, 3.0)
    assert result2 is False   # only 2 into a fresh streak


def test_anti_chase_rejects_extended_move():
    history = [
        TickSample(0.0, 100, 200, 20000),
        TickSample(1.0, 105, 195, 20005),
        TickSample(2.0, 130, 185, 20010),
    ]
    result = check_not_extended(history, "CE", lookback_seconds=5.0, max_extension_pct=15.0)
    assert result is not None
    assert "ENTRY_TOO_EXTENDED" in result


def test_anti_chase_allows_modest_move():
    history = [
        TickSample(0.0, 100, 200, 20000),
        TickSample(1.0, 103, 197, 20003),
        TickSample(2.0, 108, 192, 20008),
    ]
    result = check_not_extended(history, "CE", lookback_seconds=5.0, max_extension_pct=15.0)
    assert result is None


def test_scores_favor_ce_in_bullish_scenario():
    params = RotationParams(underlying_confirm_min_pct=0.01)
    history = [
        TickSample(0.0, 100, 225, 20000),
        TickSample(1.0, 135, 188, 20035),
    ]
    feats = calculate_window_features(history, window_seconds=1.0)
    scores = compute_scores(feats, params)
    assert scores["ce_score"] > scores["pe_score"]


def test_division_by_zero_protection():
    """A zero or negative price must never crash feature calculation."""
    history = [
        TickSample(0.0, 0.0, 200, 20000),
        TickSample(1.0, 5.0, 195, 20005),
    ]
    result = calculate_window_features(history, window_seconds=1.0)
    assert result is None   # explicitly refused, not a crash or a fabricated value
