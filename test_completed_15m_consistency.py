"""Regression tests for end-time-based 15-minute candle selection."""

from types import SimpleNamespace

import pandas as pd

from strategy import completed_15m_rows, evaluate, latest_completed_15m_row


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


def stock_row(date, direction, *, adx=30.0):
    if direction == "UP":
        close, ema_fast, ema_slow, vwap = 105.0, 103.0, 101.0, 102.0
    else:
        close, ema_fast, ema_slow, vwap = 95.0, 97.0, 99.0, 98.0

    return {
        "date": pd.Timestamp(date),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 10_000,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "vwap": vwap,
        "adx": adx,
    }


def index_row(date, direction):
    row = stock_row(date, direction)
    row["vwap"] = float("nan")
    return row


def valid_buy_5m():
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-08-10 10:00:00"),
                "open": 99.5,
                "high": 100.8,
                "low": 99.0,
                "close": 100.3,
                "volume": 1_000,
                "avg_volume": 1_000,
                "ema_entry": 100.0,
            },
            {
                "date": pd.Timestamp("2026-08-10 10:05:00"),
                "open": 100.3,
                "high": 101.7,
                "low": 100.2,
                "close": 101.5,
                "volume": 2_000,
                "avg_volume": 1_000,
                "ema_entry": 100.0,
            },
        ]
    )


cfg = SimpleNamespace(
    USE_ADX_FILTER=False,
    ADX_MODE="off",
    ADX_THRESHOLD=25,
    VOLUME_MULTIPLIER=1.5,
    SL_BUFFER_PCT=0.1,
    SL_BUFFER_PCT_SELL=None,
    RISK_REWARD_MIN=2.0,
    ENTRY_EMA=20,
    ENABLE_200_EMA_FILTER=False,
    ENABLE_VWAP_ACCEPTANCE_FILTER=False,
)


rows = pd.DataFrame(
    [
        stock_row("2026-08-10 09:45:00", "UP"),
        stock_row("2026-08-10 10:00:00", "DOWN"),
    ]
)

before_active_close = pd.Timestamp("2026-08-10 10:10:00")
completed = completed_15m_rows(rows, before_active_close)

check(
    "10:00-10:15 candle is excluded at 10:10",
    list(completed["date"]) == [pd.Timestamp("2026-08-10 09:45:00")],
)
check(
    "latest completed row is selected by candle end time",
    latest_completed_15m_row(rows, before_active_close)["date"]
    == pd.Timestamp("2026-08-10 09:45:00"),
)

stock_up_then_developing_down = rows
index_up_then_developing_down = pd.DataFrame(
    [
        index_row("2026-08-10 09:45:00", "UP"),
        index_row("2026-08-10 10:00:00", "DOWN"),
    ]
)

signal = evaluate(
    "TEST",
    stock_up_then_developing_down,
    valid_buy_5m(),
    index_up_then_developing_down,
    cfg,
)

check(
    "developing stock and NIFTY rows cannot overturn completed bullish rows",
    signal is not None and signal.direction == "BUY",
)

stock_down_then_developing_up = pd.DataFrame(
    [
        stock_row("2026-08-10 09:45:00", "DOWN"),
        stock_row("2026-08-10 10:00:00", "UP"),
    ]
)

check(
    "developing bullish stock row cannot manufacture a BUY trend",
    evaluate(
        "TEST",
        stock_down_then_developing_up,
        valid_buy_5m(),
        index_up_then_developing_down,
        cfg,
    )
    is None,
)

index_down_then_developing_up = pd.DataFrame(
    [
        index_row("2026-08-10 09:45:00", "DOWN"),
        index_row("2026-08-10 10:00:00", "UP"),
    ]
)

check(
    "developing bullish NIFTY row cannot authorize a BUY against completed bearish NIFTY",
    evaluate(
        "TEST",
        pd.DataFrame(
            [
                stock_row("2026-08-10 09:45:00", "UP"),
                stock_row("2026-08-10 10:00:00", "UP"),
            ]
        ),
        valid_buy_5m(),
        index_down_then_developing_up,
        cfg,
    )
    is None,
)

print("\nCompleted 15-minute consistency tests passed.")
