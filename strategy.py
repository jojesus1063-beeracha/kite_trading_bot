"""Strategy engine for 15-minute trend and configured entry candles."""

import json
import logging
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from scheduler import candle_interval_minutes

from adx_confidence import adx_confidence, resolve_adx_mode
from filter_diagnostics import mark_filter_status
from trend_filters import evaluate_200ema_filter, format_rejection_log
from vwap_acceptance import evaluate_vwap_acceptance, format_vwap_acceptance_log
from entry_timing import (
    evaluate_entry_timing,
    format_entry_timing_log,
    INVALID as ENTRY_TIMING_INVALID,
    NOT_ENABLED as ENTRY_TIMING_NOT_ENABLED,
)

logger = logging.getLogger("strategy")


def _gate_mode(cfg, name: str) -> str:
    mode = str(getattr(cfg, name, "enforce")).strip().lower()
    return mode if mode in {"enforce", "observe"} else "enforce"


def _baseline_trend(row_15m: pd.Series) -> Optional[str]:
    """Minimal direction source used only by paper observation mode."""
    fast = row_15m.get("ema_fast")
    slow = row_15m.get("ema_slow")
    if pd.isna(fast) or pd.isna(slow):
        return None
    if fast > slow:
        return "UP"
    if fast < slow:
        return "DOWN"
    return None


def _log_experiment_observation(symbol, direction, curr, row_15m, detail, cfg):
    candle_time = pd.Timestamp(curr["date"]).isoformat()
    observation_id = f"{candle_time}|{symbol}|{direction}"
    payload = {
        "event": "EXPERIMENTAL_ENTRY_CANDIDATE",
        "observation_id": observation_id,
        "symbol": symbol,
        "direction": direction,
        "candle_time": candle_time,
        "entry_close": float(curr["close"]),
        "trend_close": float(row_15m["close"]),
        "ema_fast": float(row_15m["ema_fast"]),
        "ema_slow": float(row_15m["ema_slow"]),
        "vwap": None if pd.isna(row_15m.get("vwap")) else float(row_15m["vwap"]),
        "adx": None if pd.isna(row_15m.get("adx")) else float(row_15m["adx"]),
        **detail,
    }
    logger.info("EXPERIMENT_OBSERVATION | %s", json.dumps(payload, sort_keys=True))
    output_path = getattr(cfg, "EXPERIMENT_OBSERVATION_FILE", None)
    if output_path:
        try:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except Exception:
            logger.exception(
                "Failed to persist experiment observation %s; journal record remains available",
                observation_id,
            )
    return observation_id


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
    raw_direction: Optional[str] = None
    final_direction: Optional[str] = None
    policy_decision: Optional[str] = None
    policy_reason: Optional[str] = None
    market_trend: Optional[str] = None


def rebuild_signal_for_direction(signal, final_direction, reference_candle, cfg):
    """
    Rebuild stop/target from the final trade direction.

    Prefer structural candle geometry when it is valid relative to
    the actual entry price. If the reference candle has already moved
    completely beyond the entry, fall back to ATR-based geometry.

    The final safety invariant is always:

        BUY  : stop < entry < target
        SELL : target < entry < stop
    """
    direction = str(final_direction).strip().upper()
    entry = float(signal.entry_price)

    if entry <= 0:
        raise ValueError(f"invalid entry price: {entry}")

    # ATR is used only as a fallback when candle structure cannot
    # produce valid geometry relative to the actual entry.
    atr = None

    try:
        atr_value = reference_candle.get("atr")
        if atr_value is not None:
            atr = float(atr_value)
            if atr <= 0:
                atr = None
    except (TypeError, ValueError, AttributeError):
        atr = None

    # Conservative fallback distance.
    # One ATR preserves volatility scaling without changing direction.
    fallback_distance = atr if atr is not None else entry * 0.005

    if direction == "BUY":
        structural_stop = (
            float(reference_candle["low"])
            * (1 - cfg.SL_BUFFER_PCT / 100)
        )

        if structural_stop < entry:
            stop = structural_stop
        else:
            stop = entry - fallback_distance

        risk = entry - stop
        target = entry + risk * cfg.RISK_REWARD_MIN

    elif direction == "SELL":
        buffer_pct = (
            getattr(cfg, "SL_BUFFER_PCT_SELL", None)
            or cfg.SL_BUFFER_PCT
        )

        structural_stop = (
            float(reference_candle["high"])
            * (1 + buffer_pct / 100)
        )

        if structural_stop > entry:
            stop = structural_stop
        else:
            stop = entry + fallback_distance

        risk = stop - entry
        target = entry - risk * cfg.RISK_REWARD_MIN

    else:
        raise ValueError(
            f"invalid final direction: {final_direction}"
        )

    # Final fail-closed safety validation.
    if direction == "BUY":
        valid = (
            stop > 0
            and stop < entry
            and target > entry
        )
    else:
        valid = (
            target > 0
            and target < entry
            and stop > entry
        )

    if risk <= 0 or not valid:
        raise ValueError(
            f"invalid {direction} geometry "
            f"entry={entry} stop={stop} target={target}"
        )

    signal.direction = direction
    signal.final_direction = direction
    signal.stop_loss = stop
    signal.target = target

    return signal


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


def completed_15m_rows(
    df_15m: pd.DataFrame,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """Return only 15-minute candles whose *end time* is at/before ``as_of``.

    Kite timestamps an OHLC candle with its start time.  Comparing that
    timestamp directly with ``as_of`` can therefore admit a still-forming
    candle.  Keep the completion rule here so every strategy consumer uses
    the same temporal boundary, even if an upstream provider accidentally
    includes a partial row.
    """
    if (
        df_15m is None
        or df_15m.empty
        or "date" not in df_15m.columns
        or as_of is None
    ):
        return pd.DataFrame()

    try:
        candle_starts = pd.to_datetime(df_15m["date"])
        decision_time = pd.Timestamp(as_of)

        candle_timezone = candle_starts.dt.tz
        if candle_timezone is not None and decision_time.tzinfo is None:
            decision_time = decision_time.tz_localize(candle_timezone)
        elif candle_timezone is None and decision_time.tzinfo is not None:
            decision_time = decision_time.tz_localize(None)
        elif candle_timezone is not None and decision_time.tzinfo is not None:
            decision_time = decision_time.tz_convert(candle_timezone)

        # Infer the actual interval. The public helper name is retained for
        # compatibility, but the proposed architecture uses 3-minute trend
        # candles and must not wait an accidental extra 12 minutes.
        ordered = candle_starts.sort_values()
        deltas = ordered.diff().dropna().dt.total_seconds().div(60)
        interval_minutes = float(deltas.median()) if not deltas.empty else 3.0
        if not 1.0 <= interval_minutes <= 60.0:
            return pd.DataFrame()
        candle_ends = candle_starts + pd.Timedelta(minutes=interval_minutes)
        return df_15m.loc[candle_ends <= decision_time]
    except (TypeError, ValueError, AttributeError):
        return pd.DataFrame()


def latest_completed_15m_row(df_15m: pd.DataFrame, as_of: pd.Timestamp):
    completed = completed_15m_rows(df_15m, as_of)
    if completed.empty:
        return None
    return completed.iloc[-1]


def latest_completed_15m_trend(
    df_15m: pd.DataFrame,
    as_of: pd.Timestamp,
    cfg=None,
) -> Optional[str]:
    row = latest_completed_15m_row(df_15m, as_of)
    if row is None:
        return None
    return get_trend(row, cfg)


def get_trend_confidence(row_15m: pd.Series, cfg=None) -> Optional[str]:
    if cfg is None:
        return None
    return adx_confidence(row_15m.get("adx"), cfg)


def latest_completed_15m_confidence(
    df_15m: pd.DataFrame,
    as_of: pd.Timestamp,
    cfg=None,
) -> Optional[str]:
    row = latest_completed_15m_row(df_15m, as_of)
    if row is None:
        return None
    return get_trend_confidence(row, cfg)


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


def _stock_adx(df_15m: pd.DataFrame, as_of_ts) -> Optional[float]:
    """The stock's own latest completed 15m ADX -- NOT the index's."""
    if df_15m is None or df_15m.empty or "date" not in df_15m.columns:
        return None
    completed = completed_15m_rows(df_15m, as_of_ts)
    if completed.empty or "adx" not in completed.columns:
        return None
    value = completed.iloc[-1].get("adx")
    return None if pd.isna(value) else float(value)


def _stock_ema_slope(df_15m: pd.DataFrame, as_of_ts) -> Optional[float]:
    """1-bar-back slope of the stock's own 15m ema_fast (EMA9 on the
    configured TREND_EMA_FAST period). Positive = rising, negative =
    falling. Requires at least 2 completed 15m bars."""
    if df_15m is None or df_15m.empty or "date" not in df_15m.columns:
        return None
    completed = completed_15m_rows(df_15m, as_of_ts)
    if len(completed) < 2 or "ema_fast" not in completed.columns:
        return None
    curr_ema = completed.iloc[-1]["ema_fast"]
    prev_ema = completed.iloc[-2]["ema_fast"]
    if pd.isna(curr_ema) or pd.isna(prev_ema):
        return None
    return float(curr_ema - prev_ema)


def _macro_authorization(macro_state: str, direction: str, df_15m: pd.DataFrame, as_of_ts, cfg) -> tuple:
    """
    Macro authorization layer.

    Only a genuine NEUTRAL macro state is treated as PASS. NEUTRAL does
    not create a signal by itself; every remaining strategy filter in
    evaluate() must still pass before a Signal is returned.

    Missing or invalid macro data must never be converted into NEUTRAL.
    Those conditions are handled before this function is called.
    """
    if macro_state == "BULLISH":
        if direction == "BUY":
            return "ALLOW", {
                "macro_state": macro_state,
                "direction": direction,
                "decision": "ALLOW_NORMAL",
            }
        return "REJECT", {
            "macro_state": macro_state,
            "direction": direction,
            "decision": "HARD_REJECT",
            "reason": "NIFTY_OPPOSING",
        }

    if macro_state == "BEARISH":
        if direction == "SELL":
            return "ALLOW", {
                "macro_state": macro_state,
                "direction": direction,
                "decision": "ALLOW_NORMAL",
            }
        return "REJECT", {
            "macro_state": macro_state,
            "direction": direction,
            "decision": "HARD_REJECT",
            "reason": "NIFTY_OPPOSING",
        }

    if macro_state == "NEUTRAL":
        return "ALLOW", {
            "macro_state": macro_state,
            "direction": direction,
            "decision": "ALLOW_NEUTRAL",
            "reason": "GENUINE_NEUTRAL_TREATED_AS_PASS",
        }

    return "REJECT", {
        "macro_state": macro_state,
        "direction": direction,
        "decision": "REJECT_INVALID_MACRO_STATE",
        "reason": "UNRECOGNIZED_MACRO_STATE",
    }


def evaluate(*args, **kwargs):
    raise RuntimeError("MATMON hooks must be installed before evaluating entries")
