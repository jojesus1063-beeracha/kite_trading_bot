"""Tests for premium_rotation_gate.py -- the entry eligibility gate."""
from fno_bot.strategies.premium_rotation import TickSample, ConfirmationTracker, RotationParams
from fno_bot.strategies.premium_rotation_gate import evaluate_entry, EntryParams


def test_rejection_reason_always_populated():
    """The single most important invariant of this module: eligible=False
    must NEVER come with an empty reason -- this is the exact silent-
    rejection bug class found in the opening scalper on 2026-08-21."""
    history = [TickSample(0.0, 100, 200, 20000)]
    tracker = ConfirmationTracker()
    result = evaluate_entry(history, 0.0, 1.0, tracker, RotationParams(), EntryParams())
    assert result.eligible is False
    assert result.reason != ""
    assert len(result.rejections) > 0


def test_gradual_rotation_becomes_eligible():
    params_rot = RotationParams(
        ce_momentum_min_pct=1.0, pe_weakness_max_pct=0.3, velocity_min=1.5,
        underlying_confirm_min_pct=0.005,
    )
    params_entry = EntryParams(score_threshold=60.0, dominance_margin=15.0, anti_chase_max_extension_pct=25.0)
    ce_seq = [100, 101.5, 103.2, 105.1, 107.3, 109.8, 112.6]
    pe_seq = [150, 148.8, 147.3, 145.6, 143.7, 141.5, 139.0]
    underlying_seq = [20000, 20003, 20007, 20011, 20016, 20021, 20027]
    tracker = ConfirmationTracker(required_count=3)
    history = []
    eligible_at = None
    for i in range(len(ce_seq)):
        t = float(i)
        history.append(TickSample(t, ce_seq[i], pe_seq[i], underlying_seq[i]))
        if len(history) < 2:
            continue
        result = evaluate_entry(history, t, 1.0, tracker, params_rot, params_entry)
        if result.eligible and eligible_at is None:
            eligible_at = t
            assert result.direction == "CE"
    assert eligible_at is not None


def test_steep_rally_rejected_by_anti_chase_despite_confirmation():
    """Documents the real tension found during testing: a very steep
    rotation can satisfy confirmation-persistence but still get
    correctly blocked by anti-chase, because by the time confirmation
    completes the price has already run too far from its local low."""
    params_rot = RotationParams(underlying_confirm_min_pct=0.01)
    params_entry = EntryParams()   # default anti_chase_max_extension_pct=15.0
    ce_seq = [100, 105, 112, 122, 135]
    pe_seq = [225, 220, 211, 200, 188]
    underlying_seq = [20000, 20005, 20012, 20022, 20035]
    tracker = ConfirmationTracker(required_count=3)
    history = []
    saw_chase_rejection = False
    for i in range(len(ce_seq)):
        t = float(i)
        history.append(TickSample(t, ce_seq[i], pe_seq[i], underlying_seq[i]))
        if len(history) < 2:
            continue
        result = evaluate_entry(history, t, 1.0, tracker, params_rot, params_entry)
        if any("ENTRY_TOO_EXTENDED" in r for r in result.rejections):
            saw_chase_rejection = True
    assert saw_chase_rejection, "expected the steep rally to eventually trip anti-chase"


def test_kill_switch_structurally_blocks_eligibility():
    """Regression test for the gap found and fixed during the initial
    build: the kill switch must prevent eligible=True outright, not
    just get logged as a warning after a trade already opened."""
    params_rot = RotationParams(
        ce_momentum_min_pct=1.0, pe_weakness_max_pct=0.3, velocity_min=1.5,
        underlying_confirm_min_pct=0.005,
    )
    params_entry = EntryParams(score_threshold=60.0, dominance_margin=15.0, anti_chase_max_extension_pct=25.0)
    ce_seq = [100, 101.5, 103.2, 105.1, 107.3, 109.8, 112.6]
    pe_seq = [150, 148.8, 147.3, 145.6, 143.7, 141.5, 139.0]
    underlying_seq = [20000, 20003, 20007, 20011, 20016, 20021, 20027]
    tracker = ConfirmationTracker(required_count=3)
    history = []
    for i in range(len(ce_seq)):
        t = float(i)
        history.append(TickSample(t, ce_seq[i], pe_seq[i], underlying_seq[i]))
        if len(history) < 2:
            continue
        result = evaluate_entry(history, t, 1.0, tracker, params_rot, params_entry,
                                 kill_switch_allowed=False, kill_switch_reason="max trades reached")
        assert result.eligible is False
        assert "kill switch" in result.reason


def test_opening_protection_structurally_blocks_eligibility():
    params_rot = RotationParams(
        ce_momentum_min_pct=1.0, pe_weakness_max_pct=0.3, velocity_min=1.5,
        underlying_confirm_min_pct=0.005,
    )
    params_entry = EntryParams(score_threshold=60.0, dominance_margin=15.0, anti_chase_max_extension_pct=25.0)
    ce_seq = [100, 101.5, 103.2, 105.1, 107.3, 109.8, 112.6]
    pe_seq = [150, 148.8, 147.3, 145.6, 143.7, 141.5, 139.0]
    underlying_seq = [20000, 20003, 20007, 20011, 20016, 20021, 20027]
    tracker = ConfirmationTracker(required_count=3)
    history = []
    for i in range(len(ce_seq)):
        t = float(i)
        history.append(TickSample(t, ce_seq[i], pe_seq[i], underlying_seq[i]))
        if len(history) < 2:
            continue
        result = evaluate_entry(history, t, 1.0, tracker, params_rot, params_entry, opening_protected=True)
        assert result.eligible is False
        assert "opening-market protection" in result.reason


def test_no_rotation_direction_rejected_cleanly():
    history = [
        TickSample(0.0, 100, 150, 20000),
        TickSample(1.0, 100.1, 150.1, 20000.1),
    ]
    tracker = ConfirmationTracker()
    result = evaluate_entry(history, 1.0, 1.0, tracker, RotationParams(), EntryParams())
    assert result.eligible is False
    assert "not a directional rotation" in result.reason
