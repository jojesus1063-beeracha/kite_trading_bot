"""Runnable PAPER orchestration using Zerodha read-only data."""
import logging
import time
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from fno_bot.instruments.contract_master import load_contract_master
from fno_bot.market_data.tick_store import TickStore
from fno_bot.market_data.ticker import FnoTicker
from .config import OptionBuyingConfig
from .engine import OptionBuyingEngine, UnderlyingSignal
from .market_data import best_ask_from_quote, best_bid_from_quote, ltp_from_quote, quote_key
from .resolver import OptionRejection
from .signal_source import EquitySignalLogSource
from .state import restore_state, save_state, save_status

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("fno.option_buying_v1")


class OptionBuyingPaperOrchestrator:
    def __init__(
        self, *, kite, api_key: str, access_token: str,
        config: OptionBuyingConfig = None, audit_fn: Optional[Callable] = None,
        ticker=None, now_fn=None, sleep_fn=None,
    ):
        self.config = config or OptionBuyingConfig()
        self.config.validate()
        self.audit = audit_fn or (lambda _event, **_data: None)
        self.kite = kite
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.sleep_fn = sleep_fn or time.sleep
        self.tick_store = TickStore()
        self.ticker = ticker or FnoTicker(api_key, access_token, self.tick_store)
        if hasattr(self.ticker, "tick_store"):
            self.tick_store = self.ticker.tick_store
        self.engine = OptionBuyingEngine(self.config, self.audit)
        self.signal_source = EquitySignalLogSource(
            self.config.signal_log_dir,
            require_executed=self.config.require_executed_equity_signal,
            audit_fn=self.audit,
        )
        self.contracts = []
        self.contract_by_symbol = {}
        self.prepared = False
        restore_state(self.config.state_path, engine=self.engine, signal_source=self.signal_source)

    def prepare(self) -> None:
        now = self.now_fn()
        self.contracts = load_contract_master(
            self.kite, self.config.exchange, as_of=now.date()
        )
        self.contract_by_symbol = {row.tradingsymbol: row for row in self.contracts}
        self.ticker.connect(threaded=True)
        if not self.ticker.wait_connected(timeout_seconds=15):
            raise RuntimeError("F&O v1 WebSocket did not connect within 15 seconds")
        tokens = [
            self.contract_by_symbol[position.tradingsymbol].instrument_token
            for position in self.engine.open_positions
            if position.tradingsymbol in self.contract_by_symbol
        ]
        if tokens:
            self.ticker.subscribe(tokens, mode="full")
        self.prepared = True
        self.audit("OPTION_V1_PAPER_READY", contracts=len(self.contracts), restored_positions=len(tokens))
        self._persist("READY", now)

    def _quote(self, exchange: str, tradingsymbol: str) -> tuple[dict, str]:
        key = quote_key(exchange, tradingsymbol)
        return self.kite.quote([key]), key

    def _current_spot(self, symbol: str) -> Optional[float]:
        key = quote_key("NSE", symbol)
        try:
            payload = self.kite.ltp([key])
            value = float((payload.get(key) or {}).get("last_price"))
            return value if value > 0 else None
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def _entry_price(self, contract) -> Optional[float]:
        try:
            payload, key = self._quote(contract.exchange, contract.tradingsymbol)
            return best_ask_from_quote(payload, key)
        except Exception as exc:
            self.audit("OPTION_REJECT_NO_PRICE", reason=str(exc), symbol=contract.tradingsymbol)
            return None

    def _position_contract(self, position):
        return self.contract_by_symbol.get(position.tradingsymbol)

    def _live_exit_price(self, position) -> Optional[float]:
        contract = self._position_contract(position)
        if contract is None:
            return None
        tick = self.tick_store.latest(contract.instrument_token)
        if (
            tick is not None
            and self.tick_store.is_fresh(contract.instrument_token, self.config.max_tick_age_ms)
            and tick.best_bid is not None and tick.best_bid > 0
        ):
            return tick.best_bid
        try:
            payload, key = self._quote(contract.exchange, contract.tradingsymbol)
            return best_bid_from_quote(payload, key)
        except Exception:
            return None

    def _forced_exit_price(self, position, now: datetime) -> Optional[float]:
        price = self._live_exit_price(position)
        if price is not None:
            return price
        if now.strftime("%H:%M") < self.config.force_exit_retry_until:
            return None
        contract = self._position_contract(position)
        tick = self.tick_store.latest(contract.instrument_token) if contract else None
        fallback = tick.last_price if tick and tick.last_price > 0 else position.lowest_option_price_after_entry
        if fallback <= 0:
            fallback = position.entry_option_price
        self.audit(
            "OPTION_FORCE_EXIT_ESTIMATED_FALLBACK", position_id=position.position_id,
            price=fallback, reason="no executable bid by final PAPER retry deadline",
        )
        return fallback

    def _observe_positions(self, now: datetime) -> None:
        for position in self.engine.open_positions:
            contract = self._position_contract(position)
            tick = self.tick_store.latest(contract.instrument_token) if contract else None
            if tick is not None and tick.last_price > 0:
                self.engine.observe(position.position_id, tick.last_price, now)

    def _consume_signals(self, now: datetime) -> None:
        for signal in self.signal_source.poll(now):
            age = (now - signal.generated_at).total_seconds()
            if age < 0 or age > self.config.max_signal_age_seconds:
                self.audit(
                    "OPTION_REJECT_STALE_SIGNAL", symbol=signal.underlying,
                    signal_age_seconds=age,
                )
                continue
            spot = self._current_spot(signal.underlying)
            if spot is None:
                self.audit("OPTION_REJECT_NO_PRICE", symbol=signal.underlying, reason="no live NSE spot")
                continue
            live_signal = UnderlyingSignal(
                signal.underlying, signal.direction, spot, signal.generated_at,
            )
            try:
                position = self.engine.submit_signal(
                    live_signal, contracts=self.contracts,
                    instrument_master_as_of=now.date(), price_fn=self._entry_price,
                )
            except OptionRejection:
                continue
            contract = self._position_contract(position)
            if contract is None:
                raise RuntimeError("resolved option absent from current NFO master")
            self.ticker.subscribe([contract.instrument_token], mode="full")

    def _persist(self, state: str, now: datetime) -> None:
        save_state(
            self.config.state_path, engine=self.engine,
            seen_signal_ids=self.signal_source.seen_ids,
        )
        save_status(
            self.config.status_path, state=state, engine=self.engine,
            socket_state="CONNECTED" if self.ticker.is_connected() else "DISCONNECTED",
            now=now,
        )

    def run_once(self, now: datetime = None) -> None:
        if not self.prepared:
            raise RuntimeError("prepare() must complete before run_once()")
        now = now or self.now_fn()
        self._observe_positions(now)
        if now.strftime("%H:%M") >= self.config.force_square_off_time:
            self.engine.force_square_off(now, lambda position: self._forced_exit_price(position, now))
        else:
            self._consume_signals(now)
        self._persist("MONITORING" if self.engine.open_positions else "WAITING", now)

    def run_forever(self) -> None:
        self.prepare()
        self.audit("OPTION_V1_PAPER_START")
        try:
            while True:
                now = self.now_fn()
                self.run_once(now)
                if now.strftime("%H:%M") >= self.config.force_exit_retry_until and not self.engine.open_positions:
                    break
                self.sleep_fn(self.config.poll_seconds)
        finally:
            self._persist("STOPPED", self.now_fn())
            self.ticker.close()
            self.audit("OPTION_V1_PAPER_STOP")
