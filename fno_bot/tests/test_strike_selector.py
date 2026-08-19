from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.strike_selector import round_to_strike_interval, select_atm_contracts


def _contract(strike, itype, expiry=date(2026, 8, 25)):
    return ContractRecord(
        tradingsymbol=f"SENSEX{expiry.strftime('%y%m%d')}{int(strike)}{itype}",
        exchange="BFO", instrument_token=hash((strike, itype)) % 1_000_000,
        name="SENSEX", expiry=expiry, strike=strike, instrument_type=itype,
        lot_size=10, tick_size=0.05, segment="BFO-OPT",
    )


def test_round_to_strike_interval_matches_spec_example():
    # Spec's observed example: SENSEX = 77,218.05 -> selected strike = 77,200
    assert round_to_strike_interval(77218.05, 100) == 77200


def test_round_to_strike_interval_rounds_up_when_closer():
    assert round_to_strike_interval(77260, 100) == 77300


def test_round_to_strike_interval_rejects_non_positive_interval():
    import pytest
    with pytest.raises(ValueError):
        round_to_strike_interval(100.0, 0)


def test_select_atm_contracts_dynamic_not_hardcoded():
    expiry = date(2026, 8, 25)
    records = [_contract(77200, "CE", expiry), _contract(77200, "PE", expiry),
               _contract(77300, "CE", expiry), _contract(77300, "PE", expiry)]
    result = select_atm_contracts(records, underlying_price=77218.05, expiry=expiry, strike_interval=100)
    assert result is not None
    assert result.atm_strike == 77200
    assert result.ce_contract.instrument_type == "CE"
    assert result.pe_contract.instrument_type == "PE"

    # A different underlying price dynamically produces a different strike --
    # proves this isn't hardcoded to 77200.
    result2 = select_atm_contracts(records, underlying_price=77260, expiry=expiry, strike_interval=100)
    assert result2.atm_strike == 77300


def test_select_atm_contracts_refuses_when_pe_missing():
    expiry = date(2026, 8, 25)
    records = [_contract(77200, "CE", expiry)]  # PE deliberately missing
    result = select_atm_contracts(records, underlying_price=77218.05, expiry=expiry, strike_interval=100)
    assert result is None


def test_select_atm_contracts_refuses_when_both_missing():
    expiry = date(2026, 8, 25)
    records = [_contract(77300, "CE", expiry), _contract(77300, "PE", expiry)]
    result = select_atm_contracts(records, underlying_price=77218.05, expiry=expiry, strike_interval=100)
    assert result is None
