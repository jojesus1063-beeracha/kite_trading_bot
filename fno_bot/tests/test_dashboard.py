import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fno_bot.dashboard import app, current_activity, summarize_activity

IST = ZoneInfo("Asia/Kolkata")


def test_summarize_activity_counts_scans_rejections_and_net_pnl():
    now = datetime(2026, 8, 26, 10, 0, tzinfo=IST)
    events = [
        {"timestamp_ist": "2026-08-26T09:59:58+05:30", "event": "WEBSOCKET_READY"},
        {"timestamp_ist": "2026-08-26T09:59:59+05:30", "event": "PROFESSIONAL_SIGNAL_EVALUATED", "symbol": "TCS", "direction": None, "reason": "ADX below 20"},
        {"timestamp_ist": "2026-08-26T10:00:00+05:30", "event": "PROFESSIONAL_SIGNAL_EVALUATED", "symbol": "INFY", "direction": "CE", "confidence": 80},
        {"timestamp_ist": "2026-08-26T10:00:00+05:30", "event": "INTRADAY_SIGNAL_EVALUATED", "symbol": "INFY", "direction": None, "reason": "ADX below 20"},
    ]
    trades = [{"date": "2026-08-26", "net_pnl": 25, "costs": 5}]
    result = summarize_activity(events, trades, {"mode": "PAPER", "state": "SCAN"}, {"positions": {}}, now)
    assert result["session"] == "INTRADAY"
    assert result["socket_state"] == "CONNECTED"
    assert result["scanned_symbols"] == 2
    assert result["evaluations"] == 3
    assert result["summary"]["net_pnl"] == 25
    assert result["rejections"][0] == {"reason": "ADX below 20", "count": 2}


def test_current_activity_reads_configured_runtime_root(tmp_path, monkeypatch):
    bot = tmp_path / "fno_bot"
    (bot / "audit_logs").mkdir(parents=True)
    today = datetime.now(IST).date().isoformat()
    (bot / "audit_logs" / f"events_{today}.jsonl").write_text(
        json.dumps({"timestamp_ist": datetime.now(IST).isoformat(), "event": "WEBSOCKET_READY"}) + "\n"
    )
    monkeypatch.setenv("FNO_DASHBOARD_RUNTIME_ROOT", str(tmp_path))
    result = current_activity()
    assert result["runtime_root"] == str(tmp_path)
    assert result["socket_state"] == "CONNECTED"


def test_dashboard_routes_are_read_only_and_return_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("FNO_DASHBOARD_RUNTIME_ROOT", str(tmp_path))
    client = app.test_client()
    assert client.get("/").status_code == 200
    response = client.get("/api/activity")
    assert response.status_code == 200
    assert "session" in response.get_json()
    assert client.post("/api/activity").status_code == 405
