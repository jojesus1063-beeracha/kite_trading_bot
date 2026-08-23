import types

from fno_bot.risk.kill_switches import FnoKillSwitch


def _cfg(**overrides):
    base = dict(MAX_TRADES_PER_DAY=3, MAX_DAILY_LOSS=1000.0, MAX_CONSECUTIVE_LOSSES=2)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_can_take_new_trade_true_initially():
    ks = FnoKillSwitch(_cfg(), persist=False)
    assert ks.can_take_new_trade() is True


def test_halts_on_max_trades_per_day():
    ks = FnoKillSwitch(_cfg(MAX_TRADES_PER_DAY=2), persist=False)
    ks.record_trade_result(10.0)
    ks.record_trade_result(10.0)
    assert ks.can_take_new_trade() is False
    assert "Max trades" in ks.day.halt_reason


def test_halts_on_daily_loss_limit():
    ks = FnoKillSwitch(_cfg(MAX_DAILY_LOSS=500.0), persist=False)
    ks.record_trade_result(-600.0)
    assert ks.can_take_new_trade() is False
    assert "Daily loss" in ks.day.halt_reason


def test_halts_on_consecutive_losses():
    ks = FnoKillSwitch(_cfg(MAX_CONSECUTIVE_LOSSES=2), persist=False)
    ks.record_trade_result(-10.0)
    ks.record_trade_result(-10.0)
    assert ks.can_take_new_trade() is False
    assert "consecutive losses" in ks.day.halt_reason


def test_consecutive_losses_reset_on_a_win():
    ks = FnoKillSwitch(_cfg(MAX_CONSECUTIVE_LOSSES=2, MAX_TRADES_PER_DAY=100), persist=False)
    ks.record_trade_result(-10.0)
    ks.record_trade_result(50.0)  # win resets the streak
    ks.record_trade_result(-10.0)
    assert ks.can_take_new_trade() is True
    assert ks.day.consecutive_losses == 1


def test_halt_does_not_apply_retroactively_to_open_position_management():
    # can_take_new_trade() governs entries only -- exit logic must never
    # consult this. This test documents that contract: the kill switch
    # object exposes no method that blocks exits at all.
    ks = FnoKillSwitch(_cfg(MAX_TRADES_PER_DAY=1), persist=False)
    ks.record_trade_result(-10.0)
    assert ks.can_take_new_trade() is False
    assert not hasattr(ks, "can_exit_position")  # no such gate exists by design
