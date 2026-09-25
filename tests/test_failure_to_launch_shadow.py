from datetime import datetime, timedelta, timezone
from pathlib import Path

from failure_to_launch_shadow import observe_failure_to_launch


def base_position(**overrides):
    now = datetime.now(timezone.utc)
    p = {
        "direction": "BUY",
        "entry": 100.0,
        "entry_time": (now - timedelta(minutes=10)).isoformat(),
        "mfe_pct": 0.05,
        "entry_context_detail": {
            "adx_current": 24.0,
            "plus_di": 25.0,
            "minus_di": 10.0,
        },
    }
    p.update(overrides)
    return p, now.timestamp()


def test_low_mfe_no_progress_flags_shadow_failure(tmp_path: Path):
    p, now = base_position()
    event = observe_failure_to_launch(
        p, "TEST", 99.95, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "FAILURE_TO_LAUNCH_SHADOW"
    assert event["SHADOW_ONLY"] is True


def test_healthy_mfe_is_not_failure(tmp_path: Path):
    p, now = base_position(mfe_pct=0.30)
    event = observe_failure_to_launch(
        p, "TEST", 100.10, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "HEALTHY_LAUNCH"


def test_strong_entry_gets_exception(tmp_path: Path):
    p, now = base_position(
        entry_context_detail={
            "adx_current": 37.0,
            "plus_di": 35.0,
            "minus_di": 10.0,
        }
    )
    event = observe_failure_to_launch(
        p, "TEST", 99.90, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "STRONG_ENTRY_EXCEPTION"


def test_low_mfe_but_positive_position_keeps_waiting(tmp_path: Path):
    p, now = base_position()
    event = observe_failure_to_launch(
        p, "TEST", 100.05, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "WAITING"
    assert not p.get("failure_to_launch_shadow_evaluated", False)


def test_before_nine_minutes_waits(tmp_path: Path):
    p, now = base_position(
        entry_time=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    )
    event = observe_failure_to_launch(
        p, "TEST", 99.0, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "WAITING"


def test_sell_direction_gap_and_failure(tmp_path: Path):
    p, now = base_position(
        direction="SELL",
        entry_context_detail={
            "adx_current": 23.0,
            "plus_di": 10.0,
            "minus_di": 28.0,
        },
    )
    event = observe_failure_to_launch(
        p, "TEST", 100.05, now_epoch=now,
        log_path=tmp_path / "events.jsonl",
    )
    assert event["status"] == "FAILURE_TO_LAUNCH_SHADOW"
    assert event["entry_di_gap"] == 18.0
