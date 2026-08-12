"""
Regression test: paper_daily_risk_guard.estimate_proposed_risk() must
validate proposed risk against the SAME stop that risk.position_size()
used to size the quantity -- not a separately reconstructed stop.

Root cause (confirmed end-to-end against real production data,
2026-08-12): when entry_plan["fixed_target_enabled"] was True, this
function recomputed a flat STOP_LOSS_PERCENT-distance stop instead of
using entry_plan["signal_stop_price"] -- the actual geometric stop
main.py passed to risk.position_size(). Whenever the geometric stop was
tighter than the flat percentage (routine on tight-range setups), the
guard validated a much larger risk than was ever actually proposed.

Real incident: VAML, 2026-08-12 09:46:43+05:30. quantity=143 was sized
by risk.position_size() for ~Rs10 of risk against a tight geometric
stop. This function then validated it against a reconstructed 0.4498%
stop (matching STOP_LOSS_PERCENT=0.45 to within rounding) instead,
producing proposed_risk=Rs305.76 -- ~30x the real sizing risk. Every
one of 1,441 entry attempts that day was rejected by the Rs250 daily
budget for this same reason; zero trades executed all session despite
the strategy generating valid signals throughout.
"""

from paper_daily_risk_guard import estimate_proposed_risk


def _entry_plan(entry, stop, *, fixed_target=True, stop_pct=0.45):
    return {
        "signal_entry_price": entry,
        "signal_stop_price": stop,
        "fixed_target_enabled": fixed_target,
        "stop_loss_percent": stop_pct,
    }


def test_real_vaml_incident_now_validates_at_sizing_risk():
    """The exact real-world case that caused the Aug 12 zero-trade day."""
    entry = 470.5
    quantity = 143
    sizing_stop = entry + (10.0 / quantity)
    plan = _entry_plan(entry, sizing_stop, fixed_target=True, stop_pct=0.45)

    result = estimate_proposed_risk("SELL", quantity, plan)

    assert result["sizing_stop"] == sizing_stop, (
        "guard must validate against the SAME stop used for sizing, "
        "not a reconstructed flat-percentage stop"
    )
    assert abs(result["proposed_risk"] - 10.0) < 0.5, (
        f"expected ~Rs10 (the real sizing risk), got Rs{result['proposed_risk']:.2f} "
        "-- this is the exact Rs305.76 regression if it fails"
    )


def test_position_sized_for_10_rupees_is_seen_as_10_rupees_buy():
    """General case, BUY direction: sizing risk and guard-validated risk must match."""
    entry = 100.0
    stop = 99.93
    risk_per_share = entry - stop
    quantity = int(10.0 / risk_per_share)

    plan = _entry_plan(entry, stop, fixed_target=True, stop_pct=0.45)
    result = estimate_proposed_risk("BUY", quantity, plan)

    assert abs(result["proposed_risk"] - risk_per_share * quantity) < 0.01
    assert result["proposed_risk"] < 15.0, (
        f"a Rs10-sized position must not be validated as Rs{result['proposed_risk']:.2f}"
    )


def test_position_sized_for_10_rupees_is_seen_as_10_rupees_sell():
    """Mirror of the BUY case for SELL direction."""
    entry = 500.0
    stop = 500.08
    risk_per_share = stop - entry
    quantity = int(10.0 / risk_per_share)

    plan = _entry_plan(entry, stop, fixed_target=True, stop_pct=0.45)
    result = estimate_proposed_risk("SELL", quantity, plan)

    assert abs(result["proposed_risk"] - risk_per_share * quantity) < 0.01
    assert result["proposed_risk"] < 15.0


def test_fixed_target_enabled_no_longer_changes_the_validated_stop():
    """The specific flag that caused the bug must no longer create a
    divergent stop -- fixed_target_enabled True vs False must validate
    identically when signal_stop_price is the same."""
    entry, stop, qty = 200.0, 199.5, 20

    plan_fixed = _entry_plan(entry, stop, fixed_target=True, stop_pct=0.45)
    plan_not_fixed = _entry_plan(entry, stop, fixed_target=False, stop_pct=0.45)

    result_fixed = estimate_proposed_risk("BUY", qty, plan_fixed)
    result_not_fixed = estimate_proposed_risk("BUY", qty, plan_not_fixed)

    assert result_fixed["proposed_risk"] == result_not_fixed["proposed_risk"]
    assert result_fixed["sizing_stop"] == result_not_fixed["sizing_stop"] == stop


def test_missing_signal_stop_price_fails_closed_not_silently():
    """No fallback reconstruction -- if the real sizing stop is missing,
    this must raise, never silently approve using a fabricated stop."""
    import pytest
    from paper_daily_risk_guard import OpenRiskUnavailable

    plan = {
        "signal_entry_price": 100.0,
        "signal_stop_price": None,
        "fixed_target_enabled": True,
        "stop_loss_percent": 0.45,
    }
    with pytest.raises(OpenRiskUnavailable):
        estimate_proposed_risk("BUY", 10, plan)


def test_zero_quantity_short_circuits_before_needing_a_stop():
    """Unchanged behavior: quantity<=0 never needs entry_plan at all."""
    result = estimate_proposed_risk("BUY", 0, None)
    assert result["proposed_risk"] == 0.0
