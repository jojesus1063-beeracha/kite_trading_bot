"""Exact market/sector/stock regime gate.

Research-parity implementation for the 3-minute EMA9/EMA21 regime test.
This module is intentionally fail-closed: if a sector is unmapped, candle
history is unavailable, or the frozen decision table has no exact cell,
the decision is SKIP.

The gate does not place orders and does not modify risk controls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd

from data_feed import fetch_candles
from market_trend import NIFTY50_TOKEN, SECTOR_INDEX_TOKENS, sector_for_symbol

REGIME_TIMEFRAME = "3minute"
REGIME_EMA_FAST = 9
REGIME_EMA_SLOW = 21
REGIME_MIN_SPREAD_PCT = 0.05


def _normalize_label(value: str) -> str:
    value = str(value).strip().upper()
    if value in {"UP", "BULLISH"}:
        return "BULLISH"
    if value in {"DOWN", "BEARISH"}:
        return "BEARISH"
    if value in {"SIDEWAYS", "NEUTRAL"}:
        return "SIDEWAYS"
    return "UNKNOWN"


def classify_ema9_21(candles: pd.DataFrame, as_of) -> str:
    """Classify using only candles available at or before *as_of*."""
    if candles is None or candles.empty or "close" not in candles.columns:
        return "UNKNOWN"

    df = candles.copy()
    ts_col = "date" if "date" in df.columns else "timestamp" if "timestamp" in df.columns else None
    if ts_col is None:
        return "UNKNOWN"

    df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
    as_of_ts = pd.Timestamp(as_of)

    series_tz = getattr(df[ts_col].dt, "tz", None)
    if series_tz is not None:
        if as_of_ts.tzinfo is None:
            as_of_ts = as_of_ts.tz_localize("Asia/Kolkata")
        else:
            as_of_ts = as_of_ts.tz_convert(series_tz)

    df = df[df[ts_col] <= as_of_ts].sort_values(ts_col)
    if len(df) < REGIME_EMA_SLOW:
        return "UNKNOWN"

    close = pd.to_numeric(df["close"], errors="coerce")
    fast = close.ewm(span=REGIME_EMA_FAST, adjust=False).mean().iloc[-1]
    slow = close.ewm(span=REGIME_EMA_SLOW, adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if pd.isna(last) or pd.isna(fast) or pd.isna(slow) or float(last) <= 0:
        return "UNKNOWN"

    spread_pct = (float(fast) - float(slow)) / float(last) * 100.0
    if spread_pct >= REGIME_MIN_SPREAD_PCT:
        return "BULLISH"
    if spread_pct <= -REGIME_MIN_SPREAD_PCT:
        return "BEARISH"
    return "SIDEWAYS"


def load_frozen_table(path: str) -> Dict[Tuple[str, str, str, str], str]:
    """Load a frozen exact-cell table generated from prior training data.

    Supported JSON shapes:
      [{"market_trend":..., "sector_trend":..., "stock_trend":...,
        "original_direction":..., "decision":"NORMAL|REVERSE|SKIP"}, ...]
    or
      {"BULLISH|SIDEWAYS|BEARISH|BUY": "REVERSE", ...}
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    table: Dict[Tuple[str, str, str, str], str] = {}

    if isinstance(raw, dict):
        for key, decision in raw.items():
            parts = str(key).split("|")
            if len(parts) != 4:
                continue
            d = str(decision).upper()
            if d in {"NORMAL", "REVERSE", "SKIP"}:
                table[tuple(_normalize_label(x) for x in parts[:3]) + (parts[3].upper(),)] = d
        return table

    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            decision = str(row.get("decision", "SKIP")).upper()
            if decision not in {"NORMAL", "REVERSE", "SKIP"}:
                continue
            key = (
                _normalize_label(row.get("market_trend", "UNKNOWN")),
                _normalize_label(row.get("sector_trend", "UNKNOWN")),
                _normalize_label(row.get("stock_trend", "UNKNOWN")),
                str(row.get("original_direction", row.get("direction", ""))).upper(),
            )
            if key[3] in {"BUY", "SELL"}:
                table[key] = decision
    return table


def _fetch_regime_candles(kite, token, lookback_days: int = 5):
    return fetch_candles(kite, token, REGIME_TIMEFRAME, lookback_days=lookback_days)


def decide_exact_regime(
    kite,
    symbol: str,
    stock_token: int,
    original_direction: str,
    signal_ts,
    frozen_table_path: str,
    market_candles: Optional[pd.DataFrame] = None,
    sector_cache: Optional[dict] = None,
):
    """Return (decision, detail). Decision is NORMAL/REVERSE/SKIP.

    Exact cells only. No market+stock or stock-only fallback is used.
    """
    table = load_frozen_table(frozen_table_path)
    if not table:
        return "SKIP", {"reason": "FROZEN_TABLE_MISSING_OR_EMPTY"}

    if market_candles is None:
        market_candles = _fetch_regime_candles(kite, NIFTY50_TOKEN)
    market = classify_ema9_21(market_candles, signal_ts)

    sector_name = sector_for_symbol(symbol)
    if sector_name is None:
        return "SKIP", {"reason": "UNMAPPED_SECTOR", "market_trend": market}
    sector_token = SECTOR_INDEX_TOKENS.get(sector_name)
    if sector_token is None:
        return "SKIP", {"reason": "SECTOR_TOKEN_MISSING", "market_trend": market, "sector": sector_name}

    if sector_cache is None:
        sector_cache = {}
    if sector_name not in sector_cache:
        sector_cache[sector_name] = _fetch_regime_candles(kite, sector_token)
    sector = classify_ema9_21(sector_cache[sector_name], signal_ts)

    stock_candles = _fetch_regime_candles(kite, stock_token)
    stock = classify_ema9_21(stock_candles, signal_ts)

    direction = str(original_direction).upper()
    key = (market, sector, stock, direction)
    decision = table.get(key, "SKIP")
    if "UNKNOWN" in key[:3]:
        decision = "SKIP"

    return decision, {
        "reason": "EXACT_CELL" if key in table else "UNSEEN_EXACT_CELL",
        "market_trend": market,
        "sector_trend": sector,
        "stock_trend": stock,
        "original_direction": direction,
        "decision": decision,
        "sector": sector_name,
    }


def reverse_direction(direction: str) -> str:
    return "SELL" if str(direction).upper() == "BUY" else "BUY"
