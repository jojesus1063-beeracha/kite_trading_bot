import replay_selective_loss_checkpoints_20260810_11 as s


def decision(name, age, mfe, mae, cur, already=False):
    return s.checkpoint_decision(
        s.CANDIDATE_BY_NAME[name], age, mfe, mae, cur, already
    )


def test_cp8_does_not_evaluate_early():
    evaluated, reason = decision("CP8_MAE15_NEG", 7.99, 0.0, -0.30, -0.20)
    assert evaluated is False
    assert reason is None


def test_cp8_exact_boundary_fires():
    evaluated, reason = decision("CP8_MAE15_NEG", 8.0, 0.20, -0.15, -0.01)
    assert evaluated is True
    assert reason == "selective_cp8_mae15_neg"


def test_cp8_requires_negative_current():
    evaluated, reason = decision("CP8_MAE15_NEG", 8.0, 0.20, -0.20, 0.01)
    assert evaluated is True
    assert reason is None


def test_checkpoint_is_one_shot_after_non_trigger():
    evaluated, reason = decision("CP8_MAE15_NEG", 8.0, 0.20, -0.10, -0.01)
    assert evaluated is True
    assert reason is None
    evaluated2, reason2 = decision("CP8_MAE15_NEG", 12.0, 0.20, -0.40, -0.20, evaluated)
    assert evaluated2 is True
    assert reason2 is None


def test_cp9_does_not_evaluate_at_8_99():
    evaluated, reason = decision("CP9_MAE15_NEG", 8.99, 0.0, -0.30, -0.20)
    assert evaluated is False
    assert reason is None


def test_cp9_exact_boundary_fires():
    evaluated, reason = decision("CP9_MAE15_NEG", 9.0, 0.40, -0.15, -0.001)
    assert evaluated is True
    assert reason == "selective_cp9_mae15_neg"


def test_cp9_low_mfe_variant_requires_mfe_strictly_below_015():
    evaluated, reason = decision("CP9_MAE15_NEG_MFE15", 9.0, 0.15, -0.20, -0.10)
    assert evaluated is True
    assert reason is None

    evaluated, reason = decision("CP9_MAE15_NEG_MFE15", 9.0, 0.1499, -0.20, -0.10)
    assert evaluated is True
    assert reason == "selective_cp9_mae15_neg_mfe15"


def test_cp9_mae20_requires_minus_020_or_worse():
    evaluated, reason = decision("CP9_MAE20_NEG", 9.0, 0.10, -0.1999, -0.10)
    assert evaluated is True
    assert reason is None

    evaluated, reason = decision("CP9_MAE20_NEG", 9.0, 0.10, -0.20, -0.10)
    assert evaluated is True
    assert reason == "selective_cp9_mae20_neg"


def test_already_evaluated_never_triggers_any_candidate():
    for candidate in s.CANDIDATES:
        evaluated, reason = s.checkpoint_decision(
            candidate,
            60.0,
            0.0,
            -5.0,
            -5.0,
            True,
        )
        assert evaluated is True
        assert reason is None
