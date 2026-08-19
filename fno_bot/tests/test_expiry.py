from datetime import date

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.instruments.expiry import available_expiries, current_expiry


def _contract(expiry):
    return ContractRecord(
        tradingsymbol="X", exchange="BFO", instrument_token=1, name="SENSEX",
        expiry=expiry, strike=100, instrument_type="CE", lot_size=10,
        tick_size=0.05, segment="BFO-OPT",
    )


def test_current_expiry_picks_nearest_upcoming():
    records = [_contract(date(2026, 8, 20)), _contract(date(2026, 8, 27)), _contract(date(2026, 9, 3))]
    assert current_expiry(records, as_of=date(2026, 8, 19)) == date(2026, 8, 20)


def test_current_expiry_includes_today_if_still_listed():
    records = [_contract(date(2026, 8, 19)), _contract(date(2026, 8, 26))]
    assert current_expiry(records, as_of=date(2026, 8, 19)) == date(2026, 8, 19)


def test_current_expiry_skips_past_expiries():
    records = [_contract(date(2026, 8, 12)), _contract(date(2026, 8, 26))]
    assert current_expiry(records, as_of=date(2026, 8, 19)) == date(2026, 8, 26)


def test_current_expiry_returns_none_when_nothing_upcoming():
    records = [_contract(date(2026, 8, 12))]
    assert current_expiry(records, as_of=date(2026, 8, 19)) is None


def test_available_expiries_sorted_and_deduped():
    records = [_contract(date(2026, 9, 3)), _contract(date(2026, 8, 20)), _contract(date(2026, 8, 20))]
    assert available_expiries(records) == [date(2026, 8, 20), date(2026, 9, 3)]
