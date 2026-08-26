"""Option-specific PAPER position telemetry and append-only reporting."""
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class OptionPosition:
    position_id: str
    underlying: str
    tradingsymbol: str
    entry_time: datetime
    entry_option_price: float
    underlying_price_at_entry: float
    strike: float
    expiry: str
    option_type: str
    lot_size: int
    quantity: int
    capital_used: float
    highest_option_price_after_entry: float
    lowest_option_price_after_entry: float
    mfe_percent: float = 0.0
    mae_percent: float = 0.0
    mfe_rupees: float = 0.0
    mae_rupees: float = 0.0
    time_of_mfe: Optional[datetime] = None
    time_of_mae: Optional[datetime] = None
    final_exit_time: Optional[datetime] = None
    final_exit_price: Optional[float] = None
    gross_pnl: Optional[float] = None
    estimated_charges: float = 0.0
    slippage: float = 0.0
    net_pnl: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.final_exit_time is None

    def observe(self, option_price: float, at: datetime) -> None:
        if not self.is_open or option_price <= 0:
            return
        if option_price > self.highest_option_price_after_entry:
            self.highest_option_price_after_entry = option_price
            self.time_of_mfe = at
        if option_price < self.lowest_option_price_after_entry:
            self.lowest_option_price_after_entry = option_price
            self.time_of_mae = at
        self.mfe_percent = (
            (self.highest_option_price_after_entry - self.entry_option_price)
            / self.entry_option_price * 100
        )
        self.mae_percent = (
            (self.lowest_option_price_after_entry - self.entry_option_price)
            / self.entry_option_price * 100
        )
        self.mfe_rupees = (
            self.highest_option_price_after_entry - self.entry_option_price
        ) * self.quantity
        self.mae_rupees = (
            self.lowest_option_price_after_entry - self.entry_option_price
        ) * self.quantity

    def close(
        self,
        *,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
        additional_slippage: float = 0.0,
    ) -> None:
        self.observe(exit_price, exit_time)
        self.final_exit_time = exit_time
        self.final_exit_price = exit_price
        self.exit_reason = exit_reason
        self.slippage += additional_slippage
        self.gross_pnl = (exit_price - self.entry_option_price) * self.quantity
        self.net_pnl = self.gross_pnl - self.estimated_charges

    def to_record(self) -> dict:
        record = asdict(self)
        for key in ("entry_time", "time_of_mfe", "time_of_mae", "final_exit_time"):
            value = record[key]
            record[key] = value.isoformat() if value is not None else None
        # Preserve the exact field labels requested for downstream MFE/MAE analysis.
        record["MFE_percent"] = record.pop("mfe_percent")
        record["MAE_percent"] = record.pop("mae_percent")
        record["MFE_rupees"] = record.pop("mfe_rupees")
        record["MAE_rupees"] = record.pop("mae_rupees")
        return record


def append_closed_position(position: OptionPosition, path: str) -> None:
    if position.is_open:
        raise ValueError("cannot persist an open position as a closed trade")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as handle:
        handle.write(json.dumps(position.to_record(), sort_keys=True) + "\n")
