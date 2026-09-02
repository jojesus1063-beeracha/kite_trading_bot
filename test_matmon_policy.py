import inspect
import time
from types import SimpleNamespace

import pandas as pd

from ws_ticker import TickBuffer
from matmon_entry_policy import evaluate_direction
from matmon_quote_confirmation import evaluate_quote_window
from matmon_microstructure import evaluate_microstructure, weighted_5_imbalance
from matmon_live_candidate_launcher import authorize_candidate, dry_run_execution_boundary
import matmon_live_candidate_launcher as live_candidate
import paper_matmon_launcher as paper_matmon


def _depth(base_bid=100.0, base_ask=100.1, bid_qty=120, ask_qty=80):
    return {
        "buy": [
            {"price": base_bid - i * 0.05, "quantity": bid_qty - i * 5}
            for i in range(5)
        ],
        "sell": [
            {"price": base_ask + i * 0.05, "quantity": ask_qty + i * 5}
            for i in range(5)
        ],
    }


def _tick(ts, bid, ask, ltp, *, bid_qty=120, ask_qty=80):
    return {
        "received_at": ts,
        "last_price": ltp,
        "depth": _depth(bid, ask, bid_qty, ask_qty),
    }


def _buffer(rows):
    b = TickBuffer()
    for row in rows:
        b.append("ABC", row)
    return b


def _cfg():
    return SimpleNamespace(
        PAPER_TRADING=True,
        ENABLE_WS_CANDLES=True,
        MATMON_QUOTE_WINDOW_SECONDS=3.0,
        MATMON_QUOTE_MAX_AGE_SECONDS=2.0,
    )


def _paper_cfg():
    return SimpleNamespace(
        MATMON_EMA_FAST=3,
        MATMON_EMA_SLOW=15,
        MATMON_DI_PERIOD=14,
        STOP_LOSS_PERCENT=0.45,
        PROFIT_TARGET_PERCENT=0.70,
    )


def _trend_df(up=True, rows=40):
    base = 100.0
    step = 0.25 if up else -0.25
    data = []
    for i in range(rows):
        close = base + step * i
        data.append({
            "date": pd.Timestamp("2026-08-31 09:15", tz="Asia/Kolkata") + pd.Timedelta(minutes=3 * i),
            "open": close - (0.10 if up else -0.10),
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "volume": 1000 + i,
        })
    return pd.DataFrame(data)


def test_01_ema3_gt_ema15_buy():
    d = evaluate_direction(ema3=101, ema15=100, plus_di=30, minus_di=15)
    assert d.accepted and d.direction == "BUY"


def test_02_ema3_lt_ema15_sell():
    d = evaluate_direction(ema3=99, ema15=100, plus_di=10, minus_di=25)
    assert d.accepted and d.direction == "SELL"


def test_03_equal_ema_rejects():
    d = evaluate_direction(ema3=100, ema15=100, plus_di=30, minus_di=15)
    assert not d.accepted and d.direction is None


def test_04_di_disagreement_rejects():
    d = evaluate_direction(ema3=101, ema15=100, plus_di=10, minus_di=20)
    assert not d.accepted and d.reason == "DI_DISAGREES"


def test_05_clean_buy_full_path_passes():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100.00, 100.10, 100.05),
        _tick(now - 2.0, 100.05, 100.15, 100.10),
        _tick(now - 0.1, 100.10, 100.20, 100.15),
    ])
    assert evaluate_quote_window(b, "ABC", "BUY", now=now).confirmed


def test_06_clean_sell_full_path_passes():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100.20, 100.30, 100.25, bid_qty=80, ask_qty=120),
        _tick(now - 2.0, 100.15, 100.25, 100.20, bid_qty=75, ask_qty=125),
        _tick(now - 0.1, 100.10, 100.20, 100.15, bid_qty=70, ask_qty=130),
    ])
    assert evaluate_quote_window(b, "ABC", "SELL", now=now).confirmed


def test_07_intermediate_retreat_rejects_buy():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100.00, 100.10, 100.05),
        _tick(now - 2.0, 99.95, 100.05, 100.00),
        _tick(now - 0.1, 100.10, 100.20, 100.15),
    ])
    assert not evaluate_quote_window(b, "ABC", "BUY", now=now).confirmed


def test_08_stale_quote_rejects():
    now = time.time()
    b = _buffer([
        _tick(now - 10.0, 100.0, 100.1, 100.05),
        _tick(now - 6.5, 100.1, 100.2, 100.15),
    ])
    e = evaluate_quote_window(b, "ABC", "BUY", now=now)
    assert not e.confirmed and e.reason == "MATMON_STALE_QUOTE"


def test_09_missing_quote_rejects():
    e = evaluate_quote_window(TickBuffer(), "ABC", "BUY", now=time.time())
    assert not e.confirmed


def test_10_buy_microstructure_all_three_positive():
    now = time.time()
    ticks = [
        _tick(now - 3.0, 100, 100.1, 100.00, bid_qty=100, ask_qty=90),
        _tick(now, 100.1, 100.2, 100.24, bid_qty=140, ask_qty=70),
    ]
    m = evaluate_microstructure("BUY", ticks)
    assert m.accepted


def test_11_sell_microstructure_all_three_negative():
    now = time.time()
    ticks = [
        _tick(now - 3.0, 100.2, 100.3, 100.24, bid_qty=90, ask_qty=100),
        _tick(now, 100.1, 100.2, 100.00, bid_qty=70, ask_qty=140),
    ]
    m = evaluate_microstructure("SELL", ticks)
    assert m.accepted


def test_12_zero_velocity_rejects():
    now = time.time()
    ticks = [
        _tick(now - 3, 100, 100.1, 100.0, bid_qty=100, ask_qty=90),
        _tick(now, 100.1, 100.2, 100.0, bid_qty=140, ask_qty=70),
    ]
    assert not evaluate_microstructure("BUY", ticks).accepted


def test_13_wrong_weighted5_direction_rejects():
    now = time.time()
    ticks = [
        _tick(now - 3, 100, 100.1, 100.0, bid_qty=80, ask_qty=120),
        _tick(now, 100.1, 100.2, 100.2, bid_qty=90, ask_qty=130),
    ]
    assert not evaluate_microstructure("BUY", ticks).accepted


def test_14_wrong_weighted5_change_rejects():
    now = time.time()
    ticks = [
        _tick(now - 3, 100, 100.1, 100.0, bid_qty=160, ask_qty=60),
        _tick(now, 100.1, 100.2, 100.2, bid_qty=120, ask_qty=80),
    ]
    assert not evaluate_microstructure("BUY", ticks).accepted


def test_15_missing_microstructure_rejects():
    assert not evaluate_microstructure("BUY", []).accepted


def test_16_rejected_candidate_cannot_reach_boundary():
    result = live_candidate.CandidateResult(False, "BUY", "REJECTED")
    assert dry_run_execution_boundary(result)["would_submit"] is False


def test_17_valid_candidate_reaches_dry_run_boundary():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100.00, 100.10, 100.00, bid_qty=100, ask_qty=90),
        _tick(now - 1.5, 100.05, 100.15, 100.10, bid_qty=120, ask_qty=80),
        _tick(now - 0.1, 100.10, 100.20, 100.24, bid_qty=140, ask_qty=70),
    ])
    result = authorize_candidate(
        tick_buffer=b, symbol="ABC", ema3=101, ema15=100,
        plus_di=30, minus_di=15, cfg_obj=_cfg(), now=now,
    )
    boundary = dry_run_execution_boundary(result)
    assert result.accepted and boundary["would_submit"] is True
    assert boundary["execution_boundary"] == "DRY_RUN_ONLY"


def test_18_no_legacy_strategy_authorization_hook():
    assert not hasattr(live_candidate, "strategy")
    assert not hasattr(live_candidate, "install_two_indicator_patch")


def test_19_matmon_reject_cannot_be_overridden():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100.0, 100.1, 100.0),
        _tick(now - 0.1, 100.1, 100.2, 100.2),
    ])
    result = authorize_candidate(
        tick_buffer=b, symbol="ABC", ema3=101, ema15=100,
        plus_di=10, minus_di=20, cfg_obj=_cfg(), now=now,
    )
    assert not result.accepted and result.reason == "DI_DISAGREES"


def test_20_paper_guard_and_no_broker_object():
    bad = _cfg(); bad.PAPER_TRADING = False
    try:
        live_candidate.assert_dry_run_contract(bad)
    except SystemExit:
        pass
    else:
        raise AssertionError("PAPER_TRADING=False must fail closed")
    assert not hasattr(live_candidate, "kite")
    assert not hasattr(live_candidate, "executor")


def test_21_weighted5_formula_prefers_near_levels():
    tick = _tick(time.time(), 100, 100.1, 100.05, bid_qty=120, ask_qty=80)
    value = weighted_5_imbalance(tick)
    assert value is not None and value > 0


def test_22_zero_denominator_fails_closed():
    tick = {
        "received_at": time.time(), "last_price": 100,
        "depth": {"buy": [{"price": 100, "quantity": 0}] * 5,
                  "sell": [{"price": 100.1, "quantity": 0}] * 5},
    }
    assert weighted_5_imbalance(tick) is None


def test_23_nonfinite_ema_fails_closed():
    d = evaluate_direction(ema3=float("nan"), ema15=100, plus_di=30, minus_di=10)
    assert not d.accepted


def test_24_microstructure_uses_same_clean_ticks():
    now = time.time()
    b = _buffer([
        _tick(now - 3.2, 100, 100.1, 100.0, bid_qty=100, ask_qty=90),
        _tick(now - 1.0, 100.05, 100.15, 100.1, bid_qty=120, ask_qty=80),
        _tick(now - 0.1, 100.1, 100.2, 100.2, bid_qty=140, ask_qty=70),
    ])
    clean = evaluate_quote_window(b, "ABC", "BUY", now=now)
    micro = evaluate_microstructure("BUY", clean.ticks)
    assert clean.confirmed and micro.sample_count == len(clean.ticks)


def test_25_paper_signal_is_direct_ema3_15_di14_buy():
    signal = paper_matmon._matmon_signal("ABC", _trend_df(up=True), _paper_cfg())
    assert signal is not None and signal.direction == "BUY"
    detail = signal.price_action_detail["matmon"]
    assert detail["ema3"] > detail["ema15"]
    assert detail["plus_di"] > detail["minus_di"]


def test_26_paper_signal_is_direct_ema3_15_di14_sell():
    signal = paper_matmon._matmon_signal("ABC", _trend_df(up=False), _paper_cfg())
    assert signal is not None and signal.direction == "SELL"
    detail = signal.price_action_detail["matmon"]
    assert detail["ema3"] < detail["ema15"]
    assert detail["minus_di"] > detail["plus_di"]


def test_27_paper_launcher_has_no_contrarian_patch_dependency():
    source = inspect.getsource(paper_matmon)
    assert "paper_contrarian_launcher" not in source
    assert "install_two_indicator_patch" not in source
    assert "ema_evaluate = strategy.evaluate" not in source


def test_28_paper_period_contract_is_3_15_14():
    assert paper_matmon.MATMON_REQUIRED["MATMON_EMA_FAST"] == 3
    assert paper_matmon.MATMON_REQUIRED["MATMON_EMA_SLOW"] == 15
    assert paper_matmon.MATMON_REQUIRED["MATMON_DI_PERIOD"] == 14


def test_29_paper_confirmation_requires_microstructure():
    # install_matmon_policy() delegates to install_matmon_hooks(), which is
    # also reused unchanged by the live launcher -- inspect where the logic
    # actually lives.
    source = inspect.getsource(paper_matmon.install_matmon_hooks)
    assert "evaluate_quote_window" in source
    assert "install_matmon_hooks" in inspect.getsource(paper_matmon.install_matmon_policy)
    assert "evaluate_microstructure(direction, clean.ticks)" in source
    assert source.index("evaluate_quote_window") < source.index("evaluate_microstructure(direction, clean.ticks)")


def test_30_paper_margin_guard_not_disabled():
    source = inspect.getsource(paper_matmon.enforce_settings)
    assert "cfg.CHECK_MARGIN_BEFORE_ENTRY = True" in source


def test_31_pre_di_ticks_cannot_satisfy_clean():
    now = time.time()
    di_passed_at = now - 1.0
    b = _buffer([
        _tick(now - 4.0, 100.00, 100.10, 100.00, bid_qty=100, ask_qty=90),
        _tick(now - 2.0, 100.05, 100.15, 100.10, bid_qty=120, ask_qty=80),
        _tick(now - 0.1, 100.10, 100.20, 100.24, bid_qty=140, ask_qty=70),
    ])
    evidence = evaluate_quote_window(
        b,
        "ABC",
        "BUY",
        now=now,
        not_before=di_passed_at,
    )
    assert not evidence.confirmed
    assert evidence.reason == "MATMON_INSUFFICIENT_QUOTE_WINDOW"


def test_32_paper_contract_forces_rest_authoritative_shadow_mode():
    assert paper_matmon.MATMON_REQUIRED["WS_CANDLE_MODE"] == "shadow"
    source = inspect.getsource(paper_matmon.enforce_settings)
    assert 'cfg.WS_CANDLE_MODE = "shadow"' in source


def test_33_paper_confirmation_consumes_post_di_timestamp():
    source = inspect.getsource(paper_matmon.install_matmon_hooks)
    assert "_MATMON_DI_PASSED_AT[symbol] = time.time()" in source
    assert "_MATMON_DI_PASSED_AT.pop(symbol, None)" in source
    assert "not_before=di_passed_at" in source
