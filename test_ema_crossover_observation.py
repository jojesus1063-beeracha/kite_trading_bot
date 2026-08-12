import json
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from strategy import evaluate


class Cfg:
    PAPER_TRADING = True
    EXPERIMENTAL_PAPER_ONLY = True
    EMA_CROSSOVER_OBSERVATION_MODE = True
    TREND_EMA_FAST = 9
    TREND_EMA_SLOW = 21
    ENTRY_TIMEFRAME = "3minute"
    NO_ENTRY_BEFORE = "09:30"
    SL_BUFFER_PCT = 0.05
    SL_BUFFER_PCT_SELL = None
    RISK_REWARD_MIN = 2.0
    VOLUME_MULTIPLIER = 1.2
    USE_ADX_FILTER = False
    ADX_MODE = "off"
    ENABLE_200_EMA_FILTER = False
    ENABLE_VWAP_ACCEPTANCE_FILTER = False
    ENABLE_ENTRY_TIMING_FILTER = False
    ENABLE_CONFIRMATION_QUALITY_FILTER = False
    ENABLE_VOLUME_ACCELERATION_FILTER = False
    ENTRY_EMA = 20
    EXPERIMENT_OBSERVATION_FILE = None


def trend_frame(previous_fast, previous_slow, current_fast, current_slow, close=80.0):
    return pd.DataFrame([
        {"date": datetime(2026, 8, 10, 9, 15), "open": close, "high": close + 1,
         "low": close - 1, "close": close, "volume": 100, "ema_fast": previous_fast,
         "ema_slow": previous_slow, "vwap": float("nan"), "adx": 5.0},
        {"date": datetime(2026, 8, 10, 9, 30), "open": close, "high": close + 1,
         "low": close - 1, "close": close, "volume": 100, "ema_fast": current_fast,
         "ema_slow": current_slow, "vwap": float("nan"), "adx": 5.0},
    ])


def entry_frame(buy=True):
    if buy:
        previous = {"open": 79.5, "high": 80.5, "low": 79.0, "close": 79.5}
        current = {"open": 79.5, "high": 80.2, "low": 79.2, "close": 80.0}
    else:
        previous = {"open": 80.5, "high": 81.0, "low": 79.5, "close": 80.5}
        current = {"open": 80.5, "high": 80.8, "low": 79.8, "close": 80.0}
    return pd.DataFrame([
        {"date": datetime(2026, 8, 10, 9, 39), **previous,
         "volume": 100, "avg_volume": 1000.0, "ema_entry": 90.0},
        {"date": datetime(2026, 8, 10, 9, 42), **current,
         "volume": 10, "avg_volume": 1000.0, "ema_entry": 90.0},
    ])


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


cfg = Cfg()
buy = evaluate("TEST", trend_frame(99, 100, 101, 100), entry_frame(True), None, cfg)
check("fresh bullish EMA9/EMA21 crossover produces BUY", buy is not None and buy.direction == "BUY")
check("price position relative to trend EMAs does not block", buy.entry_price < 99)

sell = evaluate("TEST", trend_frame(101, 100, 99, 100), entry_frame(False), None, cfg)
check("fresh bearish EMA9/EMA21 crossover produces SELL", sell is not None and sell.direction == "SELL")

aligned = evaluate("TEST", trend_frame(101, 100, 102, 100), entry_frame(True), None, cfg)
check("ordinary EMA alignment without a fresh cross does not trigger", aligned is None)

stale_entry = entry_frame(True).copy()
stale_entry["date"] = [
    datetime(2026, 8, 10, 9, 42),
    datetime(2026, 8, 10, 9, 45),
]
stale = evaluate("TEST", trend_frame(99, 100, 101, 100), stale_entry, None, cfg)
check("same completed crossover cannot retrigger on a later 3-minute scan", stale is None)

live_cfg = Cfg()
live_cfg.PAPER_TRADING = False
check("live mode fails closed", evaluate("TEST", trend_frame(99, 100, 101, 100), entry_frame(True), None, live_cfg) is None)

wrong_pair = Cfg()
wrong_pair.TREND_EMA_SLOW = 35
check("wrong EMA pair fails closed", evaluate("TEST", trend_frame(99, 100, 101, 100), entry_frame(True), None, wrong_pair) is None)

with tempfile.TemporaryDirectory() as tmp:
    logged_cfg = Cfg()
    logged_cfg.EXPERIMENT_OBSERVATION_FILE = str(Path(tmp) / "observations.jsonl")
    result = evaluate("TEST", trend_frame(99, 100, 101, 100), entry_frame(True), None, logged_cfg)
    rows = [json.loads(line) for line in Path(logged_cfg.EXPERIMENT_OBSERVATION_FILE).read_text().splitlines()]
    stages = {row.get("stage") for row in rows if row.get("event") == "EXPERIMENT_FILTER_DATAPOINT"}
    check("crossover candidate remains executable when observed filters fail", result is not None)
    check("point-by-point filter data is persisted", {
        "EMA_9_21_CROSSOVER", "PULLBACK_SEQUENCE", "MACRO_INDEX_FILTER",
        "VWAP_ACCEPTANCE", "EMA200_CONFIRMATION", "ENTRY_TIMING",
    }.issubset(stages))
    check("failed observational filters are labelled would_block", any(row.get("would_block") for row in rows))

print("EMA CROSSOVER OBSERVATION TESTS PASSED")
