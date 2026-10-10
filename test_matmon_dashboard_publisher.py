import json

from matmon_dashboard_observation import DashboardObservationPublisher
from ws_ticker import TickBuffer


def tick(received_at, bid, ask, ltp):
    return {
        "received_at": received_at,
        "last_price": ltp,
        "depth": {
            "buy": [{"price": bid, "quantity": 200}] * 5,
            "sell": [{"price": ask, "quantity": 100}] * 5,
        },
    }


def test_snapshot_contains_every_symbol_and_atomic_file(tmp_path):
    buffer = TickBuffer()
    buffer.append("ACTIVE", tick(100.0, 99.9, 100.1, 100.0))
    publisher = DashboardObservationPublisher(
        buffer,
        ["ACTIVE", "QUIET"],
        output_path=tmp_path / "live_snapshot.json",
    )
    payload = publisher.publish_once(now=101.0)
    assert [row["symbol"] for row in payload["stocks"]] == ["ACTIVE", "QUIET"]
    assert payload["stocks"][0]["quote_evidence"]["available"] is True
    assert payload["stocks"][1]["quote_evidence"]["available"] is False
    saved = json.loads((tmp_path / "live_snapshot.json").read_text())
    assert len(saved["stocks"]) == 2
