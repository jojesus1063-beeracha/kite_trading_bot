import pandas as pd

import replay_cp9_mae20_cooldowns_20260810_11 as mod


def t(text):
    return pd.Timestamp(f"2026-08-11 {text}", tz="Asia/Kolkata")


def test_baseline_never_blocks():
    blocked, remaining = mod.post_failure_blocked(
        mod.BASELINE, t("10:10:00"), t("10:00:00")
    )
    assert blocked is False
    assert remaining == 0.0


def test_no_cooldown_never_blocks():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_NO_CD, t("10:01:00"), t("10:00:00")
    )
    assert blocked is False
    assert remaining == 0.0


def test_no_prior_trigger_never_blocks():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_CD30, t("10:01:00"), None
    )
    assert blocked is False
    assert remaining == 0.0


def test_30m_blocks_before_boundary():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_CD30, t("10:29:59"), t("10:00:00")
    )
    assert blocked is True
    assert 0.0 < remaining < 1.0


def test_30m_allows_exact_boundary():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_CD30, t("10:30:00"), t("10:00:00")
    )
    assert blocked is False
    assert remaining == 0.0


def test_60m_blocks_before_boundary():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_CD60, t("10:59:59"), t("10:00:00")
    )
    assert blocked is True
    assert 0.0 < remaining < 1.0


def test_60m_allows_exact_boundary():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_CD60, t("11:00:00"), t("10:00:00")
    )
    assert blocked is False
    assert remaining == 0.0


def test_eod_blocks_any_later_entry():
    blocked, remaining = mod.post_failure_blocked(
        mod.CP9_EOD, t("15:00:00"), t("10:00:00")
    )
    assert blocked is True
    assert remaining == float("inf")


def test_latest_cp9_exit_ignores_nontriggered_trade():
    accepted = [
        {
            "symbol": "ABC",
            "replay": {
                "selective_trigger": None,
                "exit_time": t("10:00:00"),
            },
        },
        {
            "symbol": "XYZ",
            "replay": {
                "selective_trigger": "selective_cp9_mae20_neg",
                "exit_time": t("10:05:00"),
            },
        },
    ]
    assert mod.latest_cp9_exit_for_symbol(accepted, "ABC", t("10:30:00")) is None


def test_latest_cp9_exit_returns_latest_completed_same_symbol_trigger():
    accepted = [
        {
            "symbol": "ABC",
            "replay": {
                "selective_trigger": "selective_cp9_mae20_neg",
                "exit_time": t("10:00:00"),
            },
        },
        {
            "symbol": "ABC",
            "replay": {
                "selective_trigger": "selective_cp9_mae20_neg",
                "exit_time": t("11:00:00"),
            },
        },
        {
            "symbol": "ABC",
            "replay": {
                "selective_trigger": "selective_cp9_mae20_neg",
                "exit_time": t("12:00:00"),
            },
        },
    ]
    assert mod.latest_cp9_exit_for_symbol(
        accepted, "ABC", t("11:30:00")
    ) == t("11:00:00")
