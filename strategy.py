"""Strategy engine for 15-minute trend and 5-minute entries."""

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from adx_confidence import adx_confidence, resolve_adx_mode
from filter_diagnostics import mark_filter_status
from trend_filters import evaluate_200ema_filter, format_rejection_log
from vwap_acceptance import evaluate_vwap_acceptance, format_vwap_acceptance_log

logger = logging.getLogger("strategy")


@dataclass
class Signal:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    target: float
    timestamp: pd.Timestamp
    reason: str
    confidence: Optional[str] = None
    market_alignment: Optional[str] = None
    news_sentiment: Optional[str] = None
    news_headline: Optional[str] = None
    news_confidence_score: Optional[float] = None
    price_action_score: Optional[float] = None
    price_action_detail: Optional[dict] = None


def get_trend(row_15m: pd.Series, cfg=None, require_vwap: bool = True) -> Optional[str]:
    if pd.isna(row_15m["ema_slow"]):
        return None
    if require_vwap and pd.isna(row_15m["vwap"]):
        return None

    if cfg is not None and resolve_adx_mode(cfg) == "binary":
        adx_value = row_15m.get("adx")
        if pd.isna(adx_value) or adx_value < getattr(cfg, "ADX_THRESHOLD", 25):
            return None

    vwap_up_ok = True if not require_vwap else row_15m["close"] > row_15m["vwap"]
    vwap_down_ok = True if not require_vwap else row_15m["close"] < row_15m["vwap"]

    if row_15m["close"] > row_15m["ema_fast"] > row_15m["ema_slow"] and vwap_up_ok:
        return "UP"
    if row_15m["close"] < row_15m["ema_fast"] < row_15m["ema_slow"] and vwap_down_ok:
        return "DOWN"
    return None


def latest_completed_15m_row(df_15m: pd.DataFrame, as_of: pd.Timestamp):
    completed = df_15m[df_15m["date"] <= as_of]
    if completed.empty:
        return None
    return completed.iloc[-1]


def latest_completed_15m_trend(
    df_15m: pd.DataFrame,
    as_of: pd.Timestamp,
    cfg=None,
) -> Optional[str]:
    completed = df_15m[df_15m["date"] <= as_of]
    if completed.empty:
        return None
    return get_trend(completed.iloc[-1], cfg)


def get_trend_confidence(row_15m: pd.Series, cfg=None) -> Optional[str]:
    if cfg is None:
        return None
    return adx_confidence(row_15m.get("adx"), cfg)


def latest_completed_15m_confidence(
    df_15m: pd.DataFrame,
    as_of: pd.Timestamp,
    cfg=None,
) -> Optional[str]:
    completed = df_15m[df_15m["date"] <= as_of]
    if completed.empty:
        return None
    return get_trend_confidence(completed.iloc[-1], cfg)


def _passes_vwap_acceptance(symbol: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame, direction: str, cfg) -> bool:
    # vwap_acceptance.py requires a "vwap" column on df_5m, but
    # add_indicators() (indicators.py) only ever computes vwap on
    # df_15m -- df_5m never gets one. Every call here would otherwise
    # fail with "missing columns: vwap", 100% of the time, for every
    # symbol, blocking every signal before any other filter ever runs.
    #
    # Fix: reuse the already-computed 15-min VWAP value, broadcast onto
    # a COPY of df_5m (never mutate the caller's original df_5m, which
    # is used elsewhere in evaluate() and by the caller after this
    # returns) so every 5-min row in the acceptance window shares the
    # same VWAP reference -- consistent with how VWAP is used
    # everywhere else in this codebase (a single 15-min-derived value,
    # not a separate 5-min-native calculation).
    if "vwap" not in df_5m.columns:
        if df_15m is None or df_15m.empty or "vwap" not in df_15m.columns:
            status, detail = "FAIL", {"reason": "no 15-minute VWAP available to broadcast onto df_5m"}
            logger.info(format_vwap_acceptance_log(symbol, status, detail))
            return False
        df_5m = df_5m.copy()
        df_5m["vwap"] = df_15m["vwap"].iloc[-1]

    status, detail = evaluate_vwap_acceptance(df_5m, direction, cfg)
    if status == "FAIL":
        logger.info(format_vwap_acceptance_log(symbol, status, detail))
        return False
    if status == "PASS":
        logger.info(format_vwap_acceptance_log(symbol, status, detail))
    return True


def evaluate(symbol: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame, df_index_15m: pd.DataFrame, cfg) -> Optional[Signal]:
    """
    3-Step Pullback strategy (Setup / Rejection / Confirmation), replacing
    the prior momentum/breakout trigger. Preserves, unchanged and in the
    same logical position, the existing 15m trend/ADX gate, VWAP
    acceptance (_passes_vwap_acceptance), and 200 EMA confirmation
    (evaluate_200ema_filter) -- only the trigger condition and the new
    time/macro-index gates are new.

    df_index_15m is the benchmark index's (NIFTY 50) 15-minute candles,
    already enriched with indicators (see main.py: market_df_15m =
    get_cached_market_candles(), fetched once per scan cycle via
    market_trend.get_market_trend_diagnostic() and reused here, not
    re-fetched per symbol).

    NOTE on prev["vwap"]: df_5m never has a "vwap" column -- confirmed,
    add_indicators() (indicators.py) only ever computes vwap on df_15m.
    Rather than reference a column that cannot exist (which would crash
    exactly like this morning's VWAP_ACCEPTANCE bug), the current
    15m-timeframe VWAP value is used for the Setup condition's VWAP
    check, consistent with how VWAP is used everywhere else in this
    codebase.

    Calls to mark_filter_status() are observational only. They do not feed
    back into any strategy predicate or returned Signal.
    """
    if len(df_5m) < 2 or len(df_15m) < 1:
        mark_filter_status(symbol, "ENTRY_DATA", detail={"reason": "insufficient candle history"})
        return None

    curr = df_5m.iloc[-1]

    if curr["date"].time() < pd.Timestamp("09:45").time():
        mark_filter_status(symbol, "TIME_FILTER", detail={"reason": "Morning volatility settling"})
        return None

    prev = df_5m.iloc[-2]

    trend = latest_completed_15m_trend(df_15m, curr["date"], cfg)
    if trend is None:
        mark_filter_status(
            symbol,
            "TREND_OR_ADX",
            detail={"reason": "15m EMA/VWAP trend or binary ADX requirement not satisfied"},
        )
        return None
    if pd.isna(curr["avg_volume"]) or pd.isna(curr["ema_entry"]) or pd.isna(prev["ema_entry"]):
        mark_filter_status(
            symbol,
            "ENTRY_DATA",
            detail={"reason": "avg_volume or entry EMA unavailable"},
        )
        return None

    confidence = latest_completed_15m_confidence(df_15m, curr["date"], cfg)
    if resolve_adx_mode(cfg) == "dynamic" and confidence == "REJECTED":
        mark_filter_status(
            symbol,
            "TREND_OR_ADX",
            detail={"reason": "dynamic ADX confidence rejected trend"},
        )
        return None

    if df_index_15m is None or df_index_15m.empty:
        mark_filter_status(symbol, "MACRO_INDEX_FILTER", detail={"reason": "no index data available"})
        return None
    index_curr = df_index_15m.iloc[-1]
    if pd.isna(index_curr.get("vwap")) or pd.isna(index_curr.get("close")):
        mark_filter_status(symbol, "MACRO_INDEX_FILTER", detail={"reason": "index close/vwap unavailable"})
        return None
    index_is_bullish = index_curr["close"] > index_curr["vwap"]
    index_is_bearish = index_curr["close"] < index_curr["vwap"]

    current_15m_vwap = df_15m[df_15m["date"] <= curr["date"]].iloc[-1]["vwap"] if not df_15m.empty else float("nan")

    volume_ok = curr["volume"] > prev["volume"] and curr["volume"] > (curr["avg_volume"] * cfg.VOLUME_MULTIPLIER)

    if trend == "UP":
        setup = (prev["low"] <= prev["ema_entry"]) or (not pd.isna(current_15m_vwap) and prev["low"] <= current_15m_vwap)
        rejection = prev["close"] > prev["ema_entry"]
        confirmation = curr["close"] > prev["high"]

        if not (setup and rejection and confirmation and volume_ok and index_is_bullish):
            mark_filter_status(
                symbol,
                "PULLBACK_SEQUENCE",
                detail={
                    "direction": "BUY", "setup": bool(setup), "rejection": bool(rejection),
                    "confirmation": bool(confirmation), "volume_ok": bool(volume_ok),
                    "index_is_bullish": bool(index_is_bullish),
                },
            )
            return None

        if not _passes_vwap_acceptance(symbol, df_15m, df_5m, "BUY", cfg):
            mark_filter_status(
                symbol,
                "VWAP_ACCEPTANCE",
                detail={"direction": "BUY"},
            )
            return None
        ema200_status, ema200_detail = evaluate_200ema_filter(df_15m, "BUY", cfg)
        if ema200_status == "FAIL":
            logger.info(format_rejection_log(symbol, ema200_status, ema200_detail))
            mark_filter_status(
                symbol,
                "EMA200_CONFIRMATION",
                detail={"direction": "BUY", **(ema200_detail or {})},
            )
            return None
        entry = curr["close"]
        stop = prev["low"] * (1 - cfg.SL_BUFFER_PCT / 100)
        risk = entry - stop
        if risk <= 0:
            mark_filter_status(
                symbol,
                "INVALID_RISK_GEOMETRY",
                detail={"direction": "BUY"},
            )
            return None
        target = entry + risk * cfg.RISK_REWARD_MIN
        reason = (
            "3-step pullback: tested support, defended level, "
            f"breakout confirmation above EMA{cfg.ENTRY_EMA} "
            "on above-avg volume + bullish index + VWAP acceptance"
        )
        if getattr(cfg, "USE_ADX_FILTER", False):
            reason += " (ADX-confirmed trend)"
        if confidence:
            reason += f" [ADX confidence: {confidence}]"
        mark_filter_status(
            symbol,
            "STRATEGY_SIGNAL",
            detail={"direction": "BUY"},
        )
        return Signal(symbol, "BUY", entry, stop, target, curr["date"], reason, confidence=confidence)

    if trend == "DOWN":
        setup = (prev["high"] >= prev["ema_entry"]) or (not pd.isna(current_15m_vwap) and prev["high"] >= current_15m_vwap)
        rejection = prev["close"] < prev["ema_entry"]
        confirmation = curr["close"] < prev["low"]

        if not (setup and rejection and confirmation and volume_ok and index_is_bearish):
            mark_filter_status(
                symbol,
                "PULLBACK_SEQUENCE",
                detail={
                    "direction": "SELL", "setup": bool(setup), "rejection": bool(rejection),
                    "confirmation": bool(confirmation), "volume_ok": bool(volume_ok),
                    "index_is_bearish": bool(index_is_bearish),
                },
            )
            return None

        if not _passes_vwap_acceptance(symbol, df_15m, df_5m, "SELL", cfg):
            mark_filter_status(
                symbol,
                "VWAP_ACCEPTANCE",
                detail={"direction": "SELL"},
            )
            return None
        ema200_status, ema200_detail = evaluate_200ema_filter(df_15m, "SELL", cfg)
        if ema200_status == "FAIL":
            logger.info(format_rejection_log(symbol, ema200_status, ema200_detail))
            mark_filter_status(
                symbol,
                "EMA200_CONFIRMATION",
                detail={"direction": "SELL", **(ema200_detail or {})},
            )
            return None
        entry = curr["close"]
        sell_buffer = getattr(cfg, "SL_BUFFER_PCT_SELL", None) or cfg.SL_BUFFER_PCT
        stop = prev["high"] * (1 + sell_buffer / 100)
        risk = stop - entry
        if risk <= 0:
            mark_filter_status(
                symbol,
                "INVALID_RISK_GEOMETRY",
                detail={"direction": "SELL"},
            )
            return None
        target = entry - risk * cfg.RISK_REWARD_MIN
        reason = (
            "3-step pullback: tested resistance, defended level, "
            f"breakdown confirmation below EMA{cfg.ENTRY_EMA} "
            "on above-avg volume + bearish index + VWAP acceptance"
        )
        if getattr(cfg, "USE_ADX_FILTER", False):
            reason += " (ADX-confirmed trend)"
        if confidence:
            reason += f" [ADX confidence: {confidence}]"
        mark_filter_status(
            symbol,
            "STRATEGY_SIGNAL",
            detail={"direction": "SELL"},
        )
        return Signal(symbol, "SELL", entry, stop, target, curr["date"], reason, confidence=confidence)

    mark_filter_status(
        symbol,
        "ENTRY_EMA_OR_VOLUME",
        detail={"trend": trend},
    )
    return None
