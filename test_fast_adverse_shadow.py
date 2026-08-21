from pathlib import Path

from fast_adverse_shadow import adverse_r, observe_fast_adverse_shadow


def buy_position():
    return {"direction": "BUY", "entry": 100.0, "stop": 95.0, "qty": 10}


def sell_position():
    return {"direction": "SELL", "entry": 100.0, "stop": 105.0, "qty": 10}


def tick(price, received_at):
    return {"last_price": price, "received_at": received_at}


def test_adverse_r_buy_and_sell():
    assert round(adverse_r(buy_position(), 97.0), 6) == 0.6
    assert round(adverse_r(sell_position(), 103.0), 6) == 0.6
    assert adverse_r(buy_position(), 101.0) < 0
    assert adverse_r(sell_position(), 99.0) < 0


def test_arm_then_confirm_would_exit(tmp_path: Path):
    pos = buy_position()
    log = tmp_path / "events.jsonl"
    first = observe_fast_adverse_shadow(
        pos, "ABC", tick(97.0, 100.0), now_epoch=100.2, log_path=log
    )
    assert first["status"] == "ARMED"
    assert pos["fast_adverse_shadow_state"] == "ARMED"

    second = observe_fast_adverse_shadow(
        pos, "ABC", tick(96.9, 104.0), now_epoch=104.2, log_path=log
    )
    assert second["status"] == "WOULD_EXIT"
    assert pos["fast_adverse_shadow_state"] == "WOULD_EXIT"
    assert pos["qty"] == 10
    assert log.exists()
    assert len(log.read_text().strip().splitlines()) == 2


def test_recovery_disarms(tmp_path: Path):
    pos = sell_position()
    log = tmp_path / "events.jsonl"
    observe_fast_adverse_shadow(
        pos, "XYZ", tick(103.1, 10.0), now_epoch=10.1, log_path=log
    )
    assert pos["fast_adverse_shadow_state"] == "ARMED"
    result = observe_fast_adverse_shadow(
        pos, "XYZ", tick(102.4, 12.0), now_epoch=12.1, log_path=log
    )
    assert result["status"] == "DISARMED"
    assert pos["fast_adverse_shadow_state"] == "NORMAL"


def test_stale_tick_fails_safe_without_arming(tmp_path: Path):
    pos = buy_position()
    result = observe_fast_adverse_shadow(
        pos, "ABC", tick(96.0, 10.0), now_epoch=20.0,
        max_tick_age_seconds=2.0, log_path=tmp_path / "events.jsonl"
    )
    assert result["status"] == "SKIP_STALE_TICK"
    assert pos.get("fast_adverse_shadow_state") is None


def test_missing_tick_fails_safe():
    pos = buy_position()
    result = observe_fast_adverse_shadow(pos, "ABC", None, now_epoch=1.0, persist_events=False)
    assert result["status"] == "SKIP_NO_TICK"


def test_shadow_never_changes_position_quantity():
    pos = buy_position()
    observe_fast_adverse_shadow(pos, "ABC", tick(97.0, 1.0), now_epoch=1.0, persist_events=False)
    observe_fast_adverse_shadow(pos, "ABC", tick(96.8, 5.0), now_epoch=5.0, persist_events=False)
    assert pos["qty"] == 10
