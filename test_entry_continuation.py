from types import SimpleNamespace

import pandas as pd

from entry_continuation import assess_entry_continuation


def cfg(**overrides):
    base = dict(
        ENABLE_ENTRY_CONTINUATION_FILTER=True,
        ENTRY_RETEST_TOLERANCE_PCT=0.20,
        ENTRY_MAX_EXTENSION_PCT=0.60,
        ENTRY_CONTINUATION_MIN_BODY_FRACTION=0.20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def frame(rows):
    return pd.DataFrame(rows)


def test_disabled_is_inert():
    result = assess_entry_continuation(pd.DataFrame(), "BUY", cfg(ENABLE_ENTRY_CONTINUATION_FILTER=False))
    assert result.accepted is True


def test_buy_retest_and_continuation_passes():
    df = frame([
        {"open": 100.20, "high": 100.50, "low": 99.95, "close": 100.25, "ema_entry": 100.00},
        {"open": 100.30, "high": 100.90, "low": 100.20, "close": 100.75, "ema_entry": 100.20},
    ])
    result = assess_entry_continuation(df, "BUY", cfg(), vwap_reference=100.10)
    assert result.accepted is True
    assert result.detail["retest_ok"] is True
    assert result.detail["continuation_break"] is True


def test_sell_retest_and_continuation_passes():
    df = frame([
        {"open": 100.00, "high": 100.12, "low": 99.55, "close": 99.80, "ema_entry": 100.00},
        {"open": 99.75, "high": 99.85, "low": 99.10, "close": 99.30, "ema_entry": 99.70},
    ])
    result = assess_entry_continuation(df, "SELL", cfg(), vwap_reference=99.95)
    assert result.accepted is True


def test_rejects_chasing_without_retest():
    df = frame([
        {"open": 101.00, "high": 101.40, "low": 100.90, "close": 101.20, "ema_entry": 100.00},
        {"open": 101.25, "high": 101.80, "low": 101.20, "close": 101.70, "ema_entry": 100.20},
    ])
    result = assess_entry_continuation(df, "BUY", cfg(), vwap_reference=100.10)
    assert result.accepted is False
    assert result.detail["retest_ok"] is False


def test_rejects_no_continuation_break():
    df = frame([
        {"open": 100.10, "high": 100.80, "low": 99.95, "close": 100.40, "ema_entry": 100.00},
        {"open": 100.35, "high": 100.70, "low": 100.20, "close": 100.60, "ema_entry": 100.20},
    ])
    result = assess_entry_continuation(df, "BUY", cfg(), vwap_reference=100.05)
    assert result.accepted is False
    assert result.detail["continuation_break"] is False


def test_rejects_overextended_confirmation():
    df = frame([
        {"open": 100.10, "high": 100.50, "low": 99.95, "close": 100.30, "ema_entry": 100.00},
        {"open": 100.35, "high": 102.00, "low": 100.30, "close": 101.80, "ema_entry": 100.20},
    ])
    result = assess_entry_continuation(df, "BUY", cfg(ENTRY_MAX_EXTENSION_PCT=0.60), vwap_reference=100.10)
    assert result.accepted is False
    assert result.detail["extension_ok"] is False


def test_rejects_weak_confirmation_body():
    df = frame([
        {"open": 100.10, "high": 100.50, "low": 99.95, "close": 100.30, "ema_entry": 100.00},
        {"open": 100.49, "high": 100.80, "low": 100.20, "close": 100.55, "ema_entry": 100.20},
    ])
    result = assess_entry_continuation(df, "BUY", cfg(ENTRY_CONTINUATION_MIN_BODY_FRACTION=0.20), vwap_reference=100.10)
    assert result.accepted is False
    assert result.detail["body_ok"] is False


if __name__ == "__main__":
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
        print(f"PASS: {name}")
    print(f"{len(tests)}/{len(tests)} tests passed")
