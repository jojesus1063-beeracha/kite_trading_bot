from types import SimpleNamespace

import matmon_policy as launcher


def test_restart_replaces_timestamp_only_when_new_capture_starts(monkeypatch):
    calls = []
    capture = object()

    def fake_start(symbol, signal, **kwargs):
        calls.append((symbol, signal, kwargs))
        return capture

    monkeypatch.setattr(
        launcher.matmon_post_di_freeze, "maybe_start_capture", fake_start
    )
    launcher._MATMON_DI_PASSED_AT.clear()
    signal = SimpleNamespace(confidence="MATMON_EMA_DI")
    assert launcher.restart_matmon_confirmation(
        "ABC", signal, ws_engine="engine", cfg_obj="cfg", started_at=123.5
    )
    assert launcher.get_di_passed_at("ABC") == 123.5
    assert calls[0][2]["di_passed_at"] == 123.5


def test_restart_fails_closed_when_capture_cannot_start(monkeypatch):
    monkeypatch.setattr(
        launcher.matmon_post_di_freeze, "maybe_start_capture", lambda *a, **k: None
    )
    launcher._MATMON_DI_PASSED_AT.clear()
    assert not launcher.restart_matmon_confirmation(
        "ABC", object(), ws_engine=None, cfg_obj=object(), started_at=123.5
    )
    assert launcher.get_di_passed_at("ABC") is None
