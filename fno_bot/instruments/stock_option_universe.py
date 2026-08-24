"""Dynamic discovery of all NSE equity underlyings with listed options."""
from dataclasses import dataclass
from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.expiry import current_expiry
from fno_bot.instruments.strike_selector import StrikeSelection, select_nearest_listed_atm_contracts


@dataclass(frozen=True)
class StockOptionUnderlying:
    symbol: str
    instrument_token: int
    option_records: tuple[ContractRecord, ...]
    expiry: date


@dataclass(frozen=True)
class StockOptionPair:
    underlying: StockOptionUnderlying
    selection: StrikeSelection


def discover_stock_option_underlyings(
    nfo_records: list[ContractRecord],
    nse_instruments: list[dict],
    as_of: date,
) -> list[StockOptionUnderlying]:
    """Intersects NFO option names with live NSE EQ tradingsymbols.

    This excludes index options without maintaining a hardcoded list and
    automatically follows the broker's current-day tradable universe.
    """
    equity_tokens = {
        str(row.get("tradingsymbol", "")).upper(): int(row["instrument_token"])
        for row in nse_instruments
        if row.get("instrument_type") == "EQ"
        and row.get("segment") == "NSE"
        and row.get("tradingsymbol")
        and row.get("instrument_token") is not None
    }

    grouped: dict[str, list[ContractRecord]] = {}
    for record in nfo_records:
        name = record.name.upper()
        if record.instrument_type in ("CE", "PE") and name in equity_tokens:
            grouped.setdefault(name, []).append(record)

    out = []
    for symbol, records in grouped.items():
        expiry = current_expiry(records, as_of=as_of)
        if expiry is None:
            continue
        expiry_records = tuple(r for r in records if r.expiry == expiry)
        ce_strikes = {r.strike for r in expiry_records if r.instrument_type == "CE"}
        pe_strikes = {r.strike for r in expiry_records if r.instrument_type == "PE"}
        if not ce_strikes.intersection(pe_strikes):
            continue
        out.append(StockOptionUnderlying(symbol, equity_tokens[symbol], expiry_records, expiry))
    return sorted(out, key=lambda item: item.symbol)


def select_all_atm_pairs(
    underlyings: list[StockOptionUnderlying],
    spot_by_token: dict[int, float],
) -> list[StockOptionPair]:
    pairs = []
    for underlying in underlyings:
        spot = spot_by_token.get(underlying.instrument_token)
        if spot is None or spot <= 0:
            continue
        selection = select_nearest_listed_atm_contracts(
            list(underlying.option_records), spot, underlying.expiry
        )
        if selection is not None:
            pairs.append(StockOptionPair(underlying, selection))
    return pairs
