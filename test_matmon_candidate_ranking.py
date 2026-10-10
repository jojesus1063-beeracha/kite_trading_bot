from types import SimpleNamespace

from matmon_candidate_ranking import active_signal_score


def signal(ema3, ema15, plus_di, minus_di):
    return SimpleNamespace(price_action_detail={"matmon": {
        "ema3": ema3,
        "ema15": ema15,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }})


def test_active_signal_score_is_nonzero_and_strength_ordered():
    weak = active_signal_score(signal(101, 100, 25, 20))
    strong = active_signal_score(signal(103, 100, 40, 10))
    assert weak > 0
    assert strong > weak


def test_active_signal_score_is_symmetric_for_buy_and_sell():
    buy = active_signal_score(signal(102, 100, 35, 15))
    sell = active_signal_score(signal(100, 102, 15, 35))
    assert buy == sell


def test_missing_values_fail_to_neutral_priority():
    assert active_signal_score(SimpleNamespace(price_action_detail=None)) == 0.0
