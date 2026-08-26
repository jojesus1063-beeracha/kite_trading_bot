import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.market_data.tick_store import TickStore
from fno_bot.option_buying_v1.config import OptionBuyingConfig
from fno_bot.option_buying_v1.market_data import best_ask_from_quote, best_bid_from_quote
from fno_bot.option_buying_v1.orchestrator import OptionBuyingPaperOrchestrator
from fno_bot.option_buying_v1.signal_source import EquitySignalLogSource
from fno_bot.dashboard import summarize_activity

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 8, 26)


def option(kind, strike=100.0, lot_size=25):
    return ContractRecord(
        tradingsymbol=f"ABC27AUG{int(strike)}{kind}", exchange="NFO",
        instrument_token=1001 if kind == "CE" else 1002, name="ABC",
        expiry=TODAY + timedelta(days=1), strike=strike,
        instrument_type=kind, lot_size=lot_size, tick_size=0.05,
        segment="NFO-OPT",
    )


class FakeTicker:
    def __init__(self):
        self.tick_store = TickStore()
        self.subscriptions = []
        self.connected = False

    def connect(self, threaded=True):
        self.connected = True

    def wait_connected(self, timeout_seconds):
        return self.connected

    def subscribe(self, tokens, mode="full"):
        self.subscriptions.extend(tokens)

    def is_connected(self):
        return self.connected

    def close(self):
        self.connected = False


class FakeKite:
    def __init__(self, *, spot=101.0, ask=20.0, bid=19.5):
        self.spot = spot
        self.ask = ask
        self.bid = bid

    def ltp(self, keys):
        return {keys[0]: {"last_price": self.spot}}

    def quote(self, keys):
        key = keys[0]
        buy = [{"price": self.bid}] if self.bid else []
        sell = [{"price": self.ask}] if self.ask else []
        return {key: {"last_price": self.ask or self.bid, "depth": {"buy": buy, "sell": sell}}}


def config(tmp_path):
    return replace(
        OptionBuyingConfig(), signal_log_dir=str(tmp_path / "signals"),
        state_path=str(tmp_path / "state.json"), status_path=str(tmp_path / "status.json"),
        trade_log_path=str(tmp_path / "trades.jsonl"),
    )


def write_signal(cfg, now, *, executed=True, timestamp=None):
    directory = __import__("pathlib").Path(cfg.signal_log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": (timestamp or now).isoformat(), "symbol": "ABC",
        "direction": "BUY", "entry_price": 100.0, "executed": executed,
    }
    (directory / f"signals_{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")


def runner(tmp_path, now, *, kite=None, events=None):
    cfg = config(tmp_path)
    events = events if events is not None else []
    instance = OptionBuyingPaperOrchestrator(
        kite=kite or FakeKite(), api_key="x" * 16, access_token="y" * 32,
        config=cfg, audit_fn=lambda event, **data: events.append((event, data)),
        ticker=FakeTicker(), now_fn=lambda: now, sleep_fn=lambda _seconds: None,
    )
    instance.contracts = [option("CE"), option("PE")]
    instance.contract_by_symbol = {row.tradingsymbol: row for row in instance.contracts}
    instance.prepared = True
    return instance, cfg, events


def test_signal_source_emits_only_accepted_equity_signals_once(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    cfg = config(tmp_path)
    write_signal(cfg, now, executed=True)
    source = EquitySignalLogSource(cfg.signal_log_dir, require_executed=True)
    signals = source.poll(now)
    assert len(signals) == 1
    assert signals[0].underlying == "ABC"
    assert source.poll(now) == []


def test_signal_source_ignores_nonexecuted_equity_signal(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    cfg = config(tmp_path)
    write_signal(cfg, now, executed=False)
    assert EquitySignalLogSource(cfg.signal_log_dir).poll(now) == []


def test_quote_parser_uses_executable_ask_and_bid():
    payload = {"NFO:X": {"last_price": 10, "depth": {
        "buy": [{"price": 9.8}], "sell": [{"price": 10.2}],
    }}}
    assert best_ask_from_quote(payload, "NFO:X") == 10.2
    assert best_bid_from_quote(payload, "NFO:X") == 9.8


def test_orchestrator_converts_current_equity_signal_to_paper_atm_ce(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    instance, cfg, events = runner(tmp_path, now)
    write_signal(cfg, now)
    instance.run_once(now)
    assert len(instance.engine.open_positions) == 1
    position = instance.engine.open_positions[0]
    assert position.option_type == "CE"
    assert position.tradingsymbol == "ABC27AUG100CE"
    assert instance.ticker.subscriptions == [1001]
    assert any(event == "OPTION_PAPER_ENTRY_FILLED" for event, _ in events)


def test_stale_signal_is_consumed_but_never_traded(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    instance, cfg, events = runner(tmp_path, now)
    write_signal(cfg, now, timestamp=now - timedelta(minutes=2))
    instance.run_once(now)
    assert instance.engine.open_positions == []
    assert any(event == "OPTION_REJECT_STALE_SIGNAL" for event, _ in events)


def test_force_exit_retries_then_closes_with_audited_paper_fallback(tmp_path):
    entry = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    kite = FakeKite(ask=20.0, bid=None)
    instance, cfg, events = runner(tmp_path, entry, kite=kite)
    write_signal(cfg, entry)
    instance.run_once(entry)
    instance.run_once(datetime(2026, 8, 26, 15, 10, tzinfo=IST))
    assert len(instance.engine.open_positions) == 1
    instance.run_once(datetime(2026, 8, 26, 15, 15, tzinfo=IST))
    assert instance.engine.open_positions == []
    assert instance.engine.closed_positions[-1].exit_reason == "FNO_FORCE_SQUARE_OFF_15_10"
    assert any(event == "OPTION_FORCE_EXIT_ESTIMATED_FALLBACK" for event, _ in events)


def test_state_restart_restores_position_limits_and_consumed_signals(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    first, cfg, _ = runner(tmp_path, now)
    write_signal(cfg, now)
    first.run_once(now)
    restarted, _, _ = runner(tmp_path, now)
    assert len(restarted.engine.open_positions) == 1
    assert len(restarted.signal_source.seen_ids) == 1
    restarted.run_once(now)
    assert len(restarted.engine.open_positions) == 1


def test_status_is_dashboard_readable_and_contains_paper_capital(tmp_path):
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    instance, cfg, _ = runner(tmp_path, now)
    instance.run_once(now)
    status = json.loads(__import__("pathlib").Path(cfg.status_path).read_text())
    assert status["mode"] == "PAPER"
    assert status["available_capital"] == 5000.0
    assert status["open_positions"] == []


def test_dashboard_uses_v1_status_socket_state_without_legacy_socket_events():
    now = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    result = summarize_activity(
        [], [], {"mode": "PAPER", "state": "WAITING", "socket_state": "CONNECTED"},
        {"positions": {}}, now=now,
    )
    assert result["socket_state"] == "CONNECTED"
