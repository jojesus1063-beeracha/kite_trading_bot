from fno_bot.strategies.options_selling import decide_short_leg, otm_strike, short_premium_move
from fno_bot.strategies.premium_rotation import RotationParams, TickSample
from fno_bot.strategies.premium_rotation_gate import EntryParams
from fno_bot.strategies.premium_rotation_exits import ExitParams
from fno_bot.strategies.premium_rotation_session import ShadowSession

def test_bullish_sells_pe_and_bearish_sells_ce():
    assert decide_short_leg("BULLISH_ROTATION").option_type == "PE"
    assert decide_short_leg("BEARISH_ROTATION").option_type == "CE"
    assert decide_short_leg("NO_DIRECTION") is None

def test_otm_strike_is_one_step_away_from_atm():
    assert otm_strike(25000, 25000, 50, "PE", 1) == 24950
    assert otm_strike(25000, 25000, 50, "CE", 1) == 25050

def test_short_premium_profit_is_price_falling():
    assert short_premium_move(100, 80) == 20
    assert short_premium_move(100, 120) == -20

def test_paper_selling_session_uses_opposite_leg():
    session = ShadowSession(
        RotationParams(ce_momentum_min_pct=1.0, pe_weakness_max_pct=0.3,
                       velocity_min=1.5, underlying_confirm_min_pct=0.005),
        EntryParams(score_threshold=60.0, dominance_margin=15.0, anti_chase_max_extension_pct=25.0),
        ExitParams(profit_target_points=5.0, stop_loss_points=20.0),
        window_seconds=1.0, confirmation_required_count=3, quantity=50,
        mode="PAPER", paper_slippage_pct=0.0, strategy_mode="SELL_PREMIUM",
    )
    ce = [100, 101.5, 103.2, 105.1]
    pe = [150, 148.8, 147.3, 145.6]
    spot = [20000, 20003, 20007, 20011]
    opened = None
    for i in range(len(ce)):
        rec = session.on_tick(TickSample(float(i), ce[i], pe[i], spot[i]))
        opened = opened or rec.trade_opened
    assert opened is not None
    assert opened["underlying_direction"] == "BULLISH_ROTATION"
    assert opened["direction"] == "PE"
    assert opened["side"] == "SHORT"
    assert session.open_position.side == "SHORT"


def test_live_still_rejected():
    try:
        ShadowSession(RotationParams(), EntryParams(), ExitParams(), mode="LIVE", strategy_mode="SELL_PREMIUM")
    except ValueError:
        pass
    else:
        raise AssertionError("LIVE must remain unavailable in this paper strategy")
