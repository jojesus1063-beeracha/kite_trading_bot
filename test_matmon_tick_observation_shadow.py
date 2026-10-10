from pathlib import Path
from types import SimpleNamespace

import matmon_tick_observation_shadow as shadow


def tick(ts, bid, ask, ltp=100.0, full=True):
    count = 5 if full else 1
    return {
        "received_at": ts,
        "last_price": ltp,
        "depth": {
            "buy": [{"price": bid - i * 0.05, "quantity": 100 + i} for i in range(count)],
            "sell": [{"price": ask + i * 0.05, "quantity": 80 + i} for i in range(count)],
        },
    }


class Buffer:
    def __init__(self, rows):
        self.rows = rows

    def ticks_received_since(self, symbol, start):
        return [row for row in self.rows if row["received_at"] >= start]

    def latest(self, symbol):
        return self.rows[-1] if self.rows else None


def test_deadline_observation_recovers_after_three_seconds_without_trading(tmp_path):
    t0 = 1000.0
    rows = [
        tick(t0 + 0.5, 100.0, 100.1, 100.05),
        tick(t0 + 2.0, 100.1, 100.2, 100.15),
        tick(t0 + 3.6, 100.2, 100.3, 100.25),
        tick(t0 + 6.0, 100.3, 100.4, 100.35),
    ]
    record, path = shadow.observe(
        "ABC", "BUY", t0, Buffer(rows),
        sleep_fn=lambda _: None,
        now_fn=lambda: t0 + 7.0,
        output_root=tmp_path,
    )
    assert record["observation_only"] is True
    assert record["production_unchanged"] is True
    assert record["available_by_seconds"] == {
        "3": False, "4": True, "5": True, "7": True,
    }
    assert record["shadow_recovered_deadline_seconds"] == 4.0
    assert record["shadow_clean_confirmed"] is True
    assert Path(path).exists()


def test_start_is_noop_for_non_matmon_signal():
    signal = SimpleNamespace(confidence="OTHER", direction="BUY")
    cfg = SimpleNamespace(MATMON_MODE=True)
    assert shadow.maybe_start_observation(
        "ABC", signal, ws_engine=None, cfg=cfg, di_passed_at=1.0,
    ) is None


def test_main_hook_is_observation_only_static_contract():
    text = Path(__file__).with_name("main.py").read_text()
    assert "matmon_tick_observation_shadow.maybe_start_observation(" in text
    module = Path(__file__).with_name("matmon_tick_observation_shadow.py").read_text()
    forbidden = ("place_order(", "place_entry_order(", "place_exit_order(")
    assert not any(token in module for token in forbidden)
