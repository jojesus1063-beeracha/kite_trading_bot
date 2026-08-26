from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from fno_bot.instruments.contract_master import ContractRecord
from fno_bot.option_buying_v1.config import OptionBuyingConfig
from fno_bot.option_buying_v1.engine import OptionBuyingEngine, UnderlyingSignal
from fno_bot.option_buying_v1.resolver import OptionContractResolver, OptionRejection


TODAY = date(2026, 8, 26)


def contract(kind="CE", strike=100.0, expiry=None, lot_size=25, symbol=None, name="ABC"):
    expiry = expiry or TODAY + timedelta(days=1)
    return ContractRecord(
        tradingsymbol=symbol or f"{name}{expiry:%d%b}{int(strike)}{kind}",
        exchange="NFO", instrument_token=hash((kind, strike, expiry, name)) % 1_000_000,
        name=name, expiry=expiry, strike=strike, instrument_type=kind,
        lot_size=lot_size, tick_size=0.05, segment="NFO-OPT",
    )


def chain(expiry=None, lot_size=25):
    return [
        contract(kind, strike, expiry, lot_size)
        for strike in (90.0, 100.0, 110.0)
        for kind in ("CE", "PE")
    ]


def resolver(config=None, events=None):
    events = events if events is not None else []
    return OptionContractResolver(
        config or OptionBuyingConfig(),
        lambda event, **data: events.append((event, data)),
    )


def resolve(direction="BUY", rows=None, spot=103.0, price=100.0, capital=5000.0,
            master_date=TODAY, events=None):
    return resolver(events=events).resolve(
        underlying="ABC", direction=direction, spot_price=spot,
        contracts=chain() if rows is None else rows, trading_date=TODAY,
        instrument_master_as_of=master_date, price_fn=lambda _row: price,
        available_capital=capital,
    )


def assert_rejected(code, callback):
    with pytest.raises(OptionRejection) as exc:
        callback()
    assert exc.value.code == code


def test_bullish_signal_resolves_to_atm_ce():
    result = resolve("BULLISH")
    assert result.option_type == "CE"
    assert result.contract.instrument_type == "CE"
    assert result.atm_strike == 100.0


def test_bearish_signal_resolves_to_atm_pe():
    result = resolve("BEARISH")
    assert result.option_type == "PE"
    assert result.contract.instrument_type == "PE"


def test_same_day_expiry_is_rejected_and_logged():
    events = []
    assert_rejected(
        "OPTION_REJECT_SAME_DAY_EXPIRY",
        lambda: resolve(rows=chain(expiry=TODAY), events=events),
    )
    assert events[-1][0] == "OPTION_REJECT_SAME_DAY_EXPIRY"


def test_next_eligible_expiry_is_selected_not_same_day():
    rows = chain(expiry=TODAY) + chain(expiry=TODAY + timedelta(days=2))
    assert resolve(rows=rows).contract.expiry == TODAY + timedelta(days=2)


def test_atm_is_closest_available_listed_strike_not_guessed_interval():
    rows = [contract(kind, strike) for strike in (95.0, 112.5) for kind in ("CE", "PE")]
    assert resolve(rows=rows, spot=108.0).atm_strike == 112.5


def test_dynamic_instrument_master_lot_size_controls_quantity():
    result = resolve(rows=chain(lot_size=37), price=20.0)
    assert result.lot_size == 37
    assert result.quantity == 37
    assert result.lots == 1


def test_five_thousand_affordability_passes_for_one_whole_lot():
    result = resolve(price=100.0, capital=5000.0)
    assert result.quantity == result.lot_size
    assert result.reserved_capital <= 5000.0


def test_one_atm_lot_above_five_thousand_is_rejected():
    assert_rejected("OPTION_REJECT_UNAFFORDABLE", lambda: resolve(price=200.0))


def test_unaffordable_atm_does_not_fall_back_to_cheaper_otm():
    prices = {90.0: 10.0, 100.0: 250.0, 110.0: 10.0}
    r = resolver()
    assert_rejected(
        "OPTION_REJECT_UNAFFORDABLE",
        lambda: r.resolve(
            underlying="ABC", direction="BUY", spot_price=101.0, contracts=chain(),
            trading_date=TODAY, instrument_master_as_of=TODAY,
            price_fn=lambda row: prices[row.strike], available_capital=5000.0,
        ),
    )


def signal(at, underlying="ABC", direction="BUY"):
    return UnderlyingSignal(underlying, direction, 103.0, at)


def submit(engine, at, rows=None, price=20.0):
    return engine.submit_signal(
        signal(at), contracts=chain() if rows is None else rows,
        instrument_master_as_of=at.date(), price_fn=lambda _row: price,
    )


def test_entry_before_0927_is_rejected():
    engine = OptionBuyingEngine()
    assert_rejected("OPTION_REJECT_OUTSIDE_ENTRY_WINDOW", lambda: submit(engine, datetime(2026, 8, 26, 9, 26)))


def test_entry_after_0959_is_rejected():
    engine = OptionBuyingEngine()
    assert_rejected("OPTION_REJECT_OUTSIDE_ENTRY_WINDOW", lambda: submit(engine, datetime(2026, 8, 26, 10, 0)))


def test_fourth_trade_of_day_is_rejected():
    engine = OptionBuyingEngine()
    at = datetime(2026, 8, 26, 9, 30)
    for _ in range(3):
        position = submit(engine, at)
        engine.close_position(position.position_id, executable_price=20.0, at=at)
    assert_rejected("OPTION_REJECT_MAX_TRADES", lambda: submit(engine, at))


def test_third_simultaneous_position_is_rejected():
    engine = OptionBuyingEngine()
    at = datetime(2026, 8, 26, 9, 30)
    submit(engine, at, price=20.0)
    submit(engine, at, price=20.0)
    assert_rejected("OPTION_REJECT_MAX_POSITIONS", lambda: submit(engine, at, price=20.0))


def test_force_square_off_at_1510_and_captures_excursions():
    engine = OptionBuyingEngine()
    position = submit(engine, datetime(2026, 8, 26, 9, 30), price=20.0)
    engine.observe(position.position_id, 24.0, datetime(2026, 8, 26, 10, 0))
    engine.observe(position.position_id, 18.0, datetime(2026, 8, 26, 11, 0))
    assert engine.force_square_off(datetime(2026, 8, 26, 15, 9), lambda _p: 22.0) == []
    closed = engine.force_square_off(datetime(2026, 8, 26, 15, 10), lambda _p: 22.0)
    assert len(closed) == 1
    record = closed[0].to_record()
    assert record["exit_reason"] == "FNO_FORCE_SQUARE_OFF_15_10"
    assert record["MFE_percent"] > 0
    assert record["MAE_percent"] < 0
    assert record["final_exit_time"].endswith("15:10:00")


def test_importing_fno_config_does_not_change_equity_config():
    import config as equity_config
    before = (
        equity_config.CAPITAL, equity_config.MAX_TRADES_PER_DAY,
        equity_config.MAX_OPEN_POSITIONS, equity_config.PAPER_TRADING,
    )
    import fno_bot.option_buying_v1.config  # noqa: F401
    after = (
        equity_config.CAPITAL, equity_config.MAX_TRADES_PER_DAY,
        equity_config.MAX_OPEN_POSITIONS, equity_config.PAPER_TRADING,
    )
    assert after == before


def test_non_paper_configuration_cannot_construct_execution_engine():
    config = replace(OptionBuyingConfig(), paper_trading=False)
    with pytest.raises(RuntimeError, match="PAPER-only"):
        OptionBuyingEngine(config)


@pytest.mark.parametrize(
    ("rows", "master_date", "price", "code"),
    [
        (chain(), TODAY - timedelta(days=1), 20.0, "OPTION_REJECT_INSTRUMENT_DATA"),
        (chain(lot_size=0), TODAY, 20.0, "OPTION_REJECT_INVALID_LOT_SIZE"),
        (chain(), TODAY, None, "OPTION_REJECT_NO_PRICE"),
        ([], TODAY, 20.0, "OPTION_REJECT_NO_ELIGIBLE_EXPIRY"),
    ],
)
def test_missing_or_invalid_nfo_data_fails_closed(rows, master_date, price, code):
    assert_rejected(code, lambda: resolve(rows=rows, master_date=master_date, price=price))


def test_atm_direction_leg_missing_does_not_fall_back_to_otm():
    rows = [
        contract("PE", 100.0),
        contract("CE", 110.0), contract("PE", 110.0),
    ]
    assert_rejected("OPTION_REJECT_NO_ATM_CONTRACT", lambda: resolve(rows=rows, spot=101.0, price=20.0))


def test_zero_available_capital_has_explicit_capital_rejection():
    assert_rejected("OPTION_REJECT_CAPITAL_CHECK", lambda: resolve(price=20.0, capital=0.0))
