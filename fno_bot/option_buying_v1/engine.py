"""PAPER-only execution coordinator for isolated NFO option buying v1."""
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Optional

from fno_bot.instruments.contract_master import ContractRecord
from .config import OptionBuyingConfig
from .position import OptionPosition, append_closed_position
from .resolver import OptionContractResolver, OptionRejection


@dataclass(frozen=True)
class UnderlyingSignal:
    underlying: str
    direction: str
    spot_price: float
    generated_at: datetime


class OptionBuyingEngine:
    """The signal producer ends at ``submit_signal``.

    No equity order objects, capital settings or position state are shared.
    This first version simulates option BUY fills only and has no automatic
    profit target/stop.  It measures excursions until an explicit close or
    the mandatory intraday square-off.
    """

    def __init__(self, config: OptionBuyingConfig = None, audit_fn: Optional[Callable] = None):
        self.config = config or OptionBuyingConfig()
        self.config.validate()
        self.audit = audit_fn or (lambda _event, **_data: None)
        self.resolver = OptionContractResolver(self.config, self.audit)
        self.positions: dict[str, OptionPosition] = {}
        self.closed_positions: list[OptionPosition] = []
        self.trades_by_date: dict[date, int] = {}
        self.realized_pnl = 0.0

    @staticmethod
    def _minute(now: datetime) -> str:
        return now.strftime("%H:%M")

    def _reject(self, code: str, reason: str, **data):
        self.audit(code, reason=reason, **data)
        raise OptionRejection(code, reason)

    @property
    def open_positions(self) -> list[OptionPosition]:
        return [position for position in self.positions.values() if position.is_open]

    @property
    def available_capital(self) -> float:
        deployed = sum(position.capital_used for position in self.open_positions)
        return max(self.config.capital + self.realized_pnl - deployed, 0.0)

    def submit_signal(
        self,
        signal: UnderlyingSignal,
        *,
        contracts: Iterable[ContractRecord],
        instrument_master_as_of: date,
        price_fn: Callable[[ContractRecord], Optional[float]],
    ) -> OptionPosition:
        minute = self._minute(signal.generated_at)
        if minute < self.config.entry_start or minute > self.config.entry_end:
            self._reject(
                "OPTION_REJECT_OUTSIDE_ENTRY_WINDOW",
                f"fresh entries are allowed only {self.config.entry_start}-{self.config.entry_end} IST",
                generated_at=signal.generated_at.isoformat(),
            )
        trade_date = signal.generated_at.date()
        if self.trades_by_date.get(trade_date, 0) >= self.config.max_trades_per_day:
            self._reject("OPTION_REJECT_MAX_TRADES", "daily F&O trade limit reached")
        if len(self.open_positions) >= self.config.max_open_positions:
            self._reject("OPTION_REJECT_MAX_POSITIONS", "F&O open-position limit reached")

        resolved = self.resolver.resolve(
            underlying=signal.underlying,
            direction=signal.direction,
            spot_price=signal.spot_price,
            contracts=contracts,
            trading_date=trade_date,
            instrument_master_as_of=instrument_master_as_of,
            price_fn=price_fn,
            available_capital=self.available_capital,
        )
        # PAPER fill includes the reserved adverse entry slippage.  No
        # broker/order API is accepted by this class, so a live order cannot
        # be placed accidentally.
        entry_price = resolved.executable_price * (1 + self.config.estimated_slippage_pct / 100)
        entry_slippage = (entry_price - resolved.executable_price) * resolved.quantity
        capital_used = entry_price * resolved.quantity + resolved.estimated_charges
        if capital_used > self.available_capital:
            self._reject("OPTION_REJECT_CAPITAL_CHECK", "simulated fill exceeds available F&O capital")

        position = OptionPosition(
            position_id=f"FNO-PAPER-{uuid.uuid4().hex[:12]}",
            underlying=resolved.underlying,
            tradingsymbol=resolved.contract.tradingsymbol,
            entry_time=signal.generated_at,
            entry_option_price=entry_price,
            underlying_price_at_entry=signal.spot_price,
            strike=resolved.atm_strike,
            expiry=resolved.contract.expiry.isoformat(),
            option_type=resolved.option_type,
            lot_size=resolved.lot_size,
            quantity=resolved.quantity,
            capital_used=capital_used,
            highest_option_price_after_entry=entry_price,
            lowest_option_price_after_entry=entry_price,
            time_of_mfe=signal.generated_at,
            time_of_mae=signal.generated_at,
            estimated_charges=resolved.estimated_charges,
            slippage=entry_slippage,
        )
        self.positions[position.position_id] = position
        self.trades_by_date[trade_date] = self.trades_by_date.get(trade_date, 0) + 1
        self.audit(
            "OPTION_PAPER_ENTRY_FILLED", position_id=position.position_id,
            tradingsymbol=position.tradingsymbol, option_type=position.option_type,
            quantity=position.quantity, capital_used=position.capital_used,
        )
        return position

    def observe(self, position_id: str, option_price: float, at: datetime) -> None:
        self.positions[position_id].observe(option_price, at)

    def close_position(
        self, position_id: str, *, executable_price: float, at: datetime,
        reason: str = "MANUAL_PAPER_EXIT",
    ) -> OptionPosition:
        position = self.positions[position_id]
        if not position.is_open:
            return position
        if executable_price <= 0:
            self._reject("OPTION_REJECT_NO_PRICE", "cannot exit without a valid executable price")
        fill_price = executable_price * (1 - self.config.estimated_slippage_pct / 100)
        exit_slippage = (executable_price - fill_price) * position.quantity
        position.close(
            exit_time=at, exit_price=fill_price, exit_reason=reason,
            additional_slippage=exit_slippage,
        )
        self.realized_pnl += position.net_pnl or 0.0
        self.closed_positions.append(position)
        append_closed_position(position, self.config.trade_log_path)
        self.audit(
            "OPTION_PAPER_EXIT_FILLED", position_id=position.position_id,
            reason=reason, gross_pnl=position.gross_pnl, net_pnl=position.net_pnl,
        )
        return position

    def force_square_off(self, now: datetime, price_fn: Callable[[OptionPosition], Optional[float]]) -> list[OptionPosition]:
        if self._minute(now) < self.config.force_square_off_time:
            return []
        closed = []
        for position in list(self.open_positions):
            price = price_fn(position)
            if price is None or price <= 0:
                self.audit(
                    "OPTION_REJECT_NO_PRICE", reason="mandatory square-off has no executable price",
                    position_id=position.position_id,
                )
                continue
            closed.append(self.close_position(
                position.position_id, executable_price=price, at=now,
                reason="FNO_FORCE_SQUARE_OFF_15_10",
            ))
        return closed
