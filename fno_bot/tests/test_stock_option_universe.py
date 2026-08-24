from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.stock_option_universe import (
    discover_stock_option_underlyings,
    select_all_atm_pairs,
)
from fno_bot.instruments.strike_selector import select_nearest_listed_atm_contracts


EXPIRY = date(2026, 8, 27)


def _option(name, strike, kind, token):
    return ContractRecord(
        tradingsymbol=f"{name}{int(strike)}{kind}", exchange="NFO",
        instrument_token=token, name=name, expiry=EXPIRY, strike=float(strike),
        instrument_type=kind, lot_size=100, tick_size=0.05, segment="NFO-OPT",
    )


def test_discovers_only_nse_equities_with_complete_option_pairs():
    records = [
        _option("RELIANCE", 3000, "CE", 1), _option("RELIANCE", 3000, "PE", 2),
        _option("NIFTY", 25000, "CE", 3), _option("NIFTY", 25000, "PE", 4),
        _option("BROKEN", 100, "CE", 5),
    ]
    equities = [
        {"tradingsymbol": "RELIANCE", "instrument_token": 101, "instrument_type": "EQ", "segment": "NSE"},
        {"tradingsymbol": "BROKEN", "instrument_token": 102, "instrument_type": "EQ", "segment": "NSE"},
    ]

    result = discover_stock_option_underlyings(records, equities, date(2026, 8, 24))

    assert [item.symbol for item in result] == ["RELIANCE"]
    assert result[0].instrument_token == 101


def test_selects_closest_listed_paired_strike_without_hardcoded_interval():
    records = [
        _option("ABC", 92.5, "CE", 1), _option("ABC", 92.5, "PE", 2),
        _option("ABC", 95.0, "CE", 3), _option("ABC", 95.0, "PE", 4),
        _option("ABC", 97.5, "CE", 5), _option("ABC", 97.5, "PE", 6),
    ]
    result = select_nearest_listed_atm_contracts(records, 96.4, EXPIRY)
    assert result is not None
    assert result.atm_strike == 97.5
    assert result.strike_interval == 2.5


def test_select_all_atm_pairs_skips_missing_spot_ticks():
    records = [_option("RELIANCE", 3000, "CE", 1), _option("RELIANCE", 3000, "PE", 2)]
    equities = [{"tradingsymbol": "RELIANCE", "instrument_token": 101,
                 "instrument_type": "EQ", "segment": "NSE"}]
    underlyings = discover_stock_option_underlyings(records, equities, date(2026, 8, 24))
    assert select_all_atm_pairs(underlyings, {}) == []
    pairs = select_all_atm_pairs(underlyings, {101: 2998.0})
    assert len(pairs) == 1
    assert pairs[0].selection.atm_strike == 3000
