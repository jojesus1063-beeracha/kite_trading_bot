"""
Position sizing for options BUYING (spec #10) -- fundamentally
different math from the equity bot's risk_manager.py, which sizes off
a per-share stop-loss DISTANCE. An options buyer's maximum loss on a
long premium position is bounded by the premium paid (it can go to
zero, but structurally not below), so sizing here is driven by:

  - how much premium outlay the trade is allowed to commit
    (MAX_CAPITAL_PER_TRADE_PCT of FNO_CAPITAL), and
  - how much of that outlay we're willing to actually lose if the
    configured stop-loss fires (MAX_RISK_PER_TRADE_PCT of FNO_CAPITAL,
    ÷ the stop-loss percentage, since losing STOP_LOSS_PCT of the
    premium is the intended worst case, not 100% of it)

then floors to whole lots, and refuses the trade entirely (spec #10:
"if the calculated quantity is below one valid lot: NO TRADE") rather
than rounding up into an oversized position.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger("fno.risk_manager")


@dataclass(frozen=True)
class SizingResult:
    lots: int
    quantity: int              # lots * lot_size
    capital_budget: float
    risk_budget: float
    premium_per_lot: float
    reason: str


def compute_quantity(
    fno_capital: float,
    max_capital_per_trade_pct: float,
    max_risk_per_trade_pct: float,
    stop_loss_pct: float,
    entry_reference_price: float,
    lot_size: int,
) -> SizingResult:
    """
    Never hard-codes a quantity (spec #10). Always respects the real
    exchange lot size for the selected contract -- `lot_size` MUST come
    from the live contract master (ContractRecord.lot_size), never a
    remembered/hardcoded constant, since lot sizes change over time.
    """
    if lot_size <= 0:
        return SizingResult(0, 0, 0.0, 0.0, entry_reference_price, "invalid lot_size <= 0")
    if entry_reference_price <= 0:
        return SizingResult(0, 0, 0.0, 0.0, entry_reference_price, "invalid entry_reference_price <= 0")
    if stop_loss_pct <= 0:
        return SizingResult(0, 0, 0.0, 0.0, entry_reference_price, "invalid stop_loss_pct <= 0")

    capital_budget = fno_capital * max_capital_per_trade_pct / 100
    risk_budget = fno_capital * max_risk_per_trade_pct / 100

    premium_per_lot = entry_reference_price * lot_size
    if premium_per_lot <= 0:
        return SizingResult(0, 0, capital_budget, risk_budget, entry_reference_price, "premium_per_lot <= 0")

    loss_per_lot_at_stop = premium_per_lot * stop_loss_pct / 100

    lots_by_capital = int(capital_budget // premium_per_lot)
    lots_by_risk = int(risk_budget // loss_per_lot_at_stop) if loss_per_lot_at_stop > 0 else lots_by_capital

    lots = max(min(lots_by_capital, lots_by_risk), 0)

    if lots < 1:
        return SizingResult(
            0, 0, capital_budget, risk_budget, premium_per_lot,
            f"NO_TRADE: computed {lots} lots (capital-limited={lots_by_capital}, "
            f"risk-limited={lots_by_risk}) -- below one valid lot"
        )

    return SizingResult(
        lots, lots * lot_size, capital_budget, risk_budget, premium_per_lot,
        f"sized {lots} lot(s) ({lots * lot_size} qty): capital-limited={lots_by_capital}, "
        f"risk-limited={lots_by_risk}"
    )
