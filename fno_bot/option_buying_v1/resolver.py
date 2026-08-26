"""Fail-closed resolver from an underlying direction to one NFO option."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

from fno_bot.instruments.contract_master import ContractRecord, load_contract_master
from .config import OptionBuyingConfig


class OptionRejection(RuntimeError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class ResolvedOption:
    underlying: str
    direction: str
    option_type: str
    contract: ContractRecord
    spot_price: float
    atm_strike: float
    executable_price: float
    lot_size: int
    lots: int
    quantity: int
    premium_notional: float
    estimated_slippage: float
    estimated_charges: float
    reserved_capital: float


def _noop_audit(_event: str, **_data) -> None:
    pass


class OptionContractResolver:
    def __init__(self, config: OptionBuyingConfig, audit_fn: Optional[Callable] = None):
        self.config = config
        self.audit = audit_fn or _noop_audit

    def _reject(self, code: str, reason: str, **data):
        self.audit(code, reason=reason, **data)
        raise OptionRejection(code, reason)

    @staticmethod
    def option_type_for_direction(direction: str) -> str:
        normalized = str(direction).strip().upper()
        if normalized in {"BUY", "BULLISH", "LONG"}:
            return "CE"
        if normalized in {"SELL", "BEARISH", "SHORT"}:
            return "PE"
        raise OptionRejection("OPTION_REJECT_INVALID_DIRECTION", f"unsupported direction {direction!r}")

    def _estimated_costs(self, premium_notional: float) -> tuple[float, float]:
        slippage = premium_notional * self.config.estimated_slippage_pct / 100
        charges = 0.0
        if self.config.reserve_estimated_charges:
            charges = max(
                self.config.min_estimated_charges,
                premium_notional * self.config.estimated_charges_pct / 100,
            )
        return slippage, charges

    def resolve(
        self,
        *,
        underlying: str,
        direction: str,
        spot_price: float,
        contracts: Iterable[ContractRecord],
        trading_date: date,
        instrument_master_as_of: date,
        price_fn: Callable[[ContractRecord], Optional[float]],
        available_capital: float,
    ) -> ResolvedOption:
        self.config.validate()
        if (
            self.config.require_current_instrument_master
            and instrument_master_as_of != trading_date
        ):
            self._reject(
                "OPTION_REJECT_INSTRUMENT_DATA",
                "NFO instrument master is missing or stale",
                master_as_of=str(instrument_master_as_of), trading_date=str(trading_date),
            )
        if not underlying or spot_price <= 0:
            self._reject("OPTION_REJECT_INSTRUMENT_DATA", "missing underlying or valid spot price")

        option_type = self.option_type_for_direction(direction)
        rows = [
            row for row in contracts
            if row.exchange == self.config.exchange
            and row.name.upper() == underlying.upper()
            and row.instrument_type in {"CE", "PE"}
        ]
        if not rows:
            self._reject("OPTION_REJECT_NO_ELIGIBLE_EXPIRY", "no valid NFO options for underlying")

        same_day = [row for row in rows if row.expiry == trading_date]
        minimum_expiry = trading_date + timedelta(days=self.config.min_dte)
        eligible = [row for row in rows if row.expiry >= minimum_expiry]
        if not eligible:
            if same_day and self.config.block_same_day_expiry:
                self._reject("OPTION_REJECT_SAME_DAY_EXPIRY", "only same-day expiry contracts are available")
            self._reject("OPTION_REJECT_NO_ELIGIBLE_EXPIRY", "no expiry meets minimum DTE")

        expiry = min(row.expiry for row in eligible)
        expiry_rows = [row for row in eligible if row.expiry == expiry]
        strikes = sorted({row.strike for row in expiry_rows if row.strike > 0})
        if not strikes:
            self._reject("OPTION_REJECT_NO_ATM_CONTRACT", "eligible expiry has no valid strikes")
        atm_strike = min(strikes, key=lambda strike: (abs(strike - spot_price), strike))
        contract = next(
            (row for row in expiry_rows if row.strike == atm_strike and row.instrument_type == option_type),
            None,
        )
        if contract is None:
            self._reject(
                "OPTION_REJECT_NO_ATM_CONTRACT",
                "directional leg is absent at the closest listed ATM strike; no OTM fallback",
                strike=atm_strike, option_type=option_type,
            )
        if contract.lot_size <= 0:
            self._reject("OPTION_REJECT_INVALID_LOT_SIZE", "instrument master lot size is invalid")

        price = price_fn(contract)
        if price is None or price <= 0:
            self._reject("OPTION_REJECT_NO_PRICE", "no current executable option price")

        capital_limit = min(float(available_capital), self.config.capital)
        if capital_limit <= 0:
            self._reject("OPTION_REJECT_CAPITAL_CHECK", "available strategy capital is not positive")
        one_lot_notional = float(price) * contract.lot_size
        one_slippage, one_charges = self._estimated_costs(one_lot_notional)
        one_lot_cost = one_lot_notional + one_slippage + one_charges
        if one_lot_cost > capital_limit:
            self._reject(
                "OPTION_REJECT_UNAFFORDABLE",
                "one whole ATM lot plus reserves exceeds available F&O capital",
                required=one_lot_cost, available=capital_limit, strike=atm_strike,
            )

        lots = self.config.lots_per_trade
        quantity = lots * contract.lot_size
        premium_notional = float(price) * quantity
        slippage, charges = self._estimated_costs(premium_notional)
        reserved = premium_notional + slippage + charges
        if reserved > capital_limit:
            self._reject("OPTION_REJECT_CAPITAL_CHECK", "cost reserves leave no affordable whole lot")

        result = ResolvedOption(
            underlying=underlying.upper(), direction=str(direction).upper(), option_type=option_type,
            contract=contract, spot_price=float(spot_price), atm_strike=atm_strike,
            executable_price=float(price), lot_size=contract.lot_size, lots=lots,
            quantity=quantity, premium_notional=premium_notional,
            estimated_slippage=slippage, estimated_charges=charges,
            reserved_capital=reserved,
        )
        self.audit(
            "OPTION_CONTRACT_RESOLVED", underlying=result.underlying,
            tradingsymbol=result.contract.tradingsymbol, option_type=result.option_type,
            strike=result.atm_strike, expiry=result.contract.expiry.isoformat(),
            lot_size=result.lot_size, quantity=result.quantity,
            reserved_capital=result.reserved_capital,
        )
        return result

    def resolve_from_kite(
        self,
        *,
        kite,
        underlying: str,
        direction: str,
        spot_price: float,
        trading_date: date,
        price_fn: Callable[[ContractRecord], Optional[float]],
        available_capital: float,
    ) -> ResolvedOption:
        """Read today's broker-owned NFO instrument master, then resolve.

        ``price_fn`` must return a current executable BUY price (normally
        best ask, not LTP).  Loading the master for ``trading_date`` means
        the existing cache can never silently reuse a prior day's metadata.
        """
        contracts = load_contract_master(kite, self.config.exchange, as_of=trading_date)
        return self.resolve(
            underlying=underlying,
            direction=direction,
            spot_price=spot_price,
            contracts=contracts,
            trading_date=trading_date,
            instrument_master_as_of=trading_date,
            price_fn=price_fn,
            available_capital=available_capital,
        )
