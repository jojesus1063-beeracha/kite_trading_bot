from fno_bot.audit.shadow_log import ShadowTracker


def test_pending_horizons_only_returns_due_and_uncaptured():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1, 2, 5), reference_ce_price=300.0, reference_pe_price=200.0)
    assert tracker.pending_horizons(0.5) == []
    assert tracker.pending_horizons(1.5) == [1]
    assert tracker.pending_horizons(6.0) == [1, 2, 5]


def test_update_captures_due_horizons_and_tracks_mfe_mae():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1, 2), reference_ce_price=300.0, reference_pe_price=200.0)
    tracker.update(now_monotonic=0.5, ce_price=310.0, pe_price=190.0)
    assert tracker.captured == {}  # nothing due yet
    tracker.update(now_monotonic=1.2, ce_price=320.0, pe_price=185.0)
    assert 1 in tracker.captured
    assert tracker.captured[1] == {"ce": 320.0, "pe": 185.0}
    record = tracker.to_record()
    assert round(record["ce_mfe_pct"], 2) == round((320.0 - 300.0) / 300.0 * 100, 2)
    assert round(record["pe_mae_pct"], 2) == round((185.0 - 200.0) / 200.0 * 100, 2)


def test_is_complete():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1,), reference_ce_price=300.0, reference_pe_price=200.0)
    assert not tracker.is_complete()
    tracker.update(now_monotonic=2.0, ce_price=300.0, pe_price=200.0)
    assert tracker.is_complete()


def test_counterfactual_outcome_target_hit():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1, 2), reference_ce_price=300.0, reference_pe_price=200.0)
    tracker.update(now_monotonic=1.0, ce_price=300.0, pe_price=200.0)
    tracker.update(now_monotonic=2.0, ce_price=330.0, pe_price=200.0)  # +10% CE
    outcome = tracker.counterfactual_outcome("CE", target_pct=10.0, stop_pct=5.0)
    assert outcome == "TARGET_HIT"


def test_counterfactual_outcome_stop_hit_before_target_in_time_order():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1, 2), reference_ce_price=300.0, reference_pe_price=200.0)
    tracker.update(now_monotonic=1.0, ce_price=280.0, pe_price=200.0)  # stop hits first (5% = 285)
    tracker.update(now_monotonic=2.0, ce_price=340.0, pe_price=200.0)  # would later hit target, but stop already fired
    outcome = tracker.counterfactual_outcome("CE", target_pct=10.0, stop_pct=5.0)
    assert outcome == "STOP_HIT"


def test_counterfactual_outcome_neither_yet():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1,), reference_ce_price=300.0, reference_pe_price=200.0)
    tracker.update(now_monotonic=1.0, ce_price=302.0, pe_price=200.0)
    outcome = tracker.counterfactual_outcome("CE", target_pct=10.0, stop_pct=5.0)
    assert outcome == "NEITHER_YET"


def test_counterfactual_outcome_none_without_data():
    tracker = ShadowTracker(start_monotonic=0.0, horizons_seconds=(1,), reference_ce_price=300.0, reference_pe_price=200.0)
    assert tracker.counterfactual_outcome("CE", target_pct=10.0, stop_pct=5.0) is None
