import replay_failed_development_variants_20260810_11 as r


def test_baseline_never_triggers():
    assert r.failed_development_reason(r.BASELINE, 50.0, 0.0, -5.0, -5.0) is None


def test_variant_a_does_not_trigger_before_10_minutes():
    assert r.failed_development_reason(r.VARIANT_A, 9.999, 0.149, -0.30, -0.10) is None


def test_variant_a_triggers_at_exact_10_minutes():
    assert r.failed_development_reason(r.VARIANT_A, 10.0, 0.149, -0.30, -0.10) == (
        "failed_development_A_10m_mfe_lt_0_15_current_neg"
    )


def test_variant_a_exact_mfe_boundary_is_not_failure():
    assert r.failed_development_reason(r.VARIANT_A, 10.0, 0.150, -0.30, -0.10) is None


def test_variant_a_current_zero_is_not_failure():
    assert r.failed_development_reason(r.VARIANT_A, 10.0, 0.149, -0.30, 0.0) is None


def test_variant_b_does_not_trigger_mae_before_8_minutes():
    assert r.failed_development_reason(r.VARIANT_B, 7.999, 0.05, -0.151, -0.10) is None


def test_variant_b_triggers_at_exact_8_minute_mae_boundary():
    assert r.failed_development_reason(r.VARIANT_B, 8.0, 0.05, -0.150, -0.10) == (
        "failed_development_B_8m_mae_m0_15"
    )


def test_variant_b_uses_variant_a_fallback_after_10_minutes():
    assert r.failed_development_reason(r.VARIANT_B, 10.0, 0.149, -0.10, -0.01) == (
        "failed_development_A_10m_mfe_lt_0_15_current_neg"
    )


def test_variant_b_mae_rule_has_priority_when_both_match():
    assert r.failed_development_reason(r.VARIANT_B, 10.0, 0.10, -0.20, -0.10) == (
        "failed_development_B_8m_mae_m0_15"
    )
