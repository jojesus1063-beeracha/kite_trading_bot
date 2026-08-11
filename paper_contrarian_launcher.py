#!/usr/bin/env python3
"""Paper-only reversed EMA9/EMA21 + RSI strategy with exhaustive audit logging.

Directional signals:
- EMA9 > EMA21 -> SELL
- EMA9 < EMA21 -> BUY
- RSI >= 70 -> BUY override
- RSI <= 30 -> SELL override
- RSI 30-70 -> PASS to reversed EMA direction

Only EMA9/EMA21 and RSI may determine the paper trade direction. Legacy
technical gates (EMA200/watchlist direction, RVOL, entry quality, CHoCH/context,
price action, relative strength, market alignment, news) are observational in
this launcher and cannot reject or re-rank a candidate. Execution/risk safety
controls in main.py/executor.py remain active: trading window, risk limits,
position limits, quantity validity, fresh-price protection, stops/targets and
square-off.
"""
from __future__ import annotations

import json
import logging
import runpy
from datetime import datetime
from pathlib import Path

import pandas as pd

import config as cfg
import strategy
import watchlist_filters
import rvol
import entry_quality
import entry_confirmation
import relative_strength
import price_action

logger = logging.getLogger("paper_contrarian_launcher")
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
AUDIT_DIR = Path(__file__).resolve().parent / "runtime" / "paper_audit"
ENTRY_AUDIT = AUDIT_DIR / "entry_audit.jsonl"
CONFIG_AUDIT = AUDIT_DIR / "session_config.json"


def _safe(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, (str, bool, int, float)):
        return v
    try:
        return float(v)
    except Exception:
        return str(v)


def _row_snapshot(row):
    if row is None:
        return {}
    out = {}
    try:
        for k, v in row.items():
            out[str(k)] = _safe(v)
    except Exception:
        pass
    return out


def _df_last(df):
    return _row_snapshot(df.iloc[-1]) if df is not None and not df.empty else {}


def _append(payload):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with ENTRY_AUDIT.open("a", encoding="utf-8") as h:
        h.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")


def _config_snapshot():
    names = [n for n in dir(cfg) if n.isupper()]
    snap = {n: _safe(getattr(cfg, n)) for n in names}
    snap["PAPER_TWO_INDICATOR_LEGACY_GATES"] = "OBSERVATIONAL_ONLY"
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_AUDIT.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")


def calculate_rsi(df, period=RSI_PERIOD):
    if df is None or df.empty or "close" not in df.columns or len(df) < period + 1:
        return None
    c = pd.to_numeric(df["close"], errors="coerce")
    d = c.diff()
    g = d.clip(lower=0.0)
    l = -d.clip(upper=0.0)
    ag = g.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    al = l.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    if pd.isna(ag.iloc[-1]) or pd.isna(al.iloc[-1]):
        return None
    if al.iloc[-1] == 0:
        return 100.0 if ag.iloc[-1] > 0 else 50.0
    rs = ag.iloc[-1] / al.iloc[-1]
    return float(100 - (100 / (1 + rs)))


def ema_direction(df):
    if df is None or df.empty or "close" not in df.columns or len(df) < EMA_SLOW:
        return None, None, None
    c = pd.to_numeric(df["close"], errors="coerce")
    e9 = c.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    e21 = c.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    if pd.isna(e9) or pd.isna(e21):
        return None, None, None
    if e9 > e21:
        return "SELL", float(e9), float(e21)
    if e9 < e21:
        return "BUY", float(e9), float(e21)
    return None, float(e9), float(e21)


def rsi_direction(rsi):
    if rsi is None:
        return None
    if rsi >= RSI_OVERBOUGHT:
        return "BUY"
    if rsi <= RSI_OVERSOLD:
        return "SELL"
    return None


def _within_entry_window(ts):
    try:
        cur = pd.Timestamp(ts).time()
        start = datetime.strptime(str(getattr(cfg, "NO_ENTRY_BEFORE", "09:25")), "%H:%M").time()
        end = datetime.strptime(str(getattr(cfg, "NO_ENTRY_AFTER", "15:00")), "%H:%M").time()
        return start <= cur <= end
    except Exception:
        return False


def _legacy_observations(df15, dfentry, index15):
    obs = {
        "stock_15m_last": _df_last(df15),
        "entry_last": _df_last(dfentry),
        "index_15m_last": _df_last(index15),
    }
    if dfentry is not None and len(dfentry) >= 2:
        obs["entry_previous"] = _row_snapshot(dfentry.iloc[-2])
    try:
        row = df15.iloc[-1]
        ef = _safe(row.get("ema_fast"))
        es = _safe(row.get("ema_slow"))
        close = _safe(row.get("close"))
        vwap = _safe(row.get("vwap"))
        adx = _safe(row.get("adx"))
        ema200 = _safe(row.get("ema200"))
        obs["legacy_filter_assessment"] = {
            "trend_ema_relation": None if ef is None or es is None else ("UP" if ef > es else "DOWN" if ef < es else "FLAT"),
            "price_vs_vwap": None if close is None or vwap is None else ("ABOVE" if close > vwap else "BELOW" if close < vwap else "AT"),
            "adx": adx,
            "ema200": ema200,
            "would_adx_pass_25": None if adx is None else adx >= 25,
            "would_price_be_above_ema200": None if close is None or ema200 is None else close > ema200,
        }
    except Exception:
        obs["legacy_filter_assessment"] = {}
    try:
        cur = dfentry.iloc[-1]
        prev = dfentry.iloc[-2]
        av = _safe(cur.get("avg_volume"))
        vol = _safe(cur.get("volume"))
        pv = _safe(prev.get("volume"))
        obs["legacy_entry_assessment"] = {
            "volume": vol,
            "previous_volume": pv,
            "avg_volume": av,
            "volume_ratio": None if not av else vol / av,
            "would_volume_pass_1_2x": None if not av or vol is None else vol > av * 1.2,
            "current_close": _safe(cur.get("close")),
            "previous_high": _safe(prev.get("high")),
            "previous_low": _safe(prev.get("low")),
        }
    except Exception:
        obs["legacy_entry_assessment"] = {}
    return obs


def install_legacy_gate_bypass():
    """Make legacy technical gates observational only for this paper process."""
    original_classify = watchlist_filters.classify_direction_eligibility
    original_rvol = rvol.passes_rvol_threshold
    original_quality = entry_quality.assess_entry_quality
    original_context = entry_confirmation.assess_entry_context
    original_relative_strength = relative_strength.assess_relative_strength
    original_price_action = price_action.evaluate_price_action

    def observational_classify(df_15m, cfg_obj):
        eligibility, detail = original_classify(df_15m, cfg_obj)
        enriched = dict(detail) if isinstance(detail, dict) else {"detail": str(detail)}
        enriched.update({
            "observational_only": True,
            "original_eligibility": eligibility,
            "paper_two_indicator_bypass": True,
        })
        return watchlist_filters.NOT_ENABLED, enriched

    def observational_rvol(df_entry, cfg_obj):
        passed, value, detail = original_rvol(df_entry, cfg_obj)
        enriched = dict(detail) if isinstance(detail, dict) else {"detail": str(detail)}
        enriched.update({
            "observational_only": True,
            "original_passed": bool(passed),
            "paper_two_indicator_bypass": True,
        })
        return True, value, enriched

    def observational_quality(signal, df_entry):
        result = original_quality(signal, df_entry)
        detail = dict(result.detail) if isinstance(result.detail, dict) else {"detail": str(result.detail)}
        detail.update({
            "observational_only": True,
            "original_accepted": bool(result.accepted),
            "original_score": result.score,
            "original_reason": result.reason,
            "paper_two_indicator_bypass": True,
        })
        return entry_quality.EntryQuality(
            accepted=True,
            score=0.0,
            reason="observational only in paper two-indicator mode",
            detail=detail,
        )

    def observational_context(signal, df_15m):
        result = original_context(signal, df_15m)
        detail = dict(result.detail) if isinstance(result.detail, dict) else {"detail": str(result.detail)}
        detail.update({
            "observational_only": True,
            "original_accepted": bool(result.accepted),
            "original_score_adjustment": result.score_adjustment,
            "original_reason": result.reason,
            "paper_two_indicator_bypass": True,
        })
        return entry_confirmation.EntryContext(
            accepted=True,
            score_adjustment=0.0,
            reason="observational only in paper two-indicator mode",
            detail=detail,
        )

    def observational_relative_strength(signal, df_15m, market_df_15m, sector_df_15m):
        result = original_relative_strength(signal, df_15m, market_df_15m, sector_df_15m)
        detail = dict(result.detail) if isinstance(result.detail, dict) else {"detail": str(result.detail)}
        detail.update({
            "observational_only": True,
            "original_score_adjustment": result.score_adjustment,
            "original_reason": result.reason,
            "paper_two_indicator_bypass": True,
        })
        return relative_strength.RelativeStrengthAssessment(
            score_adjustment=0.0,
            reason="observational only in paper two-indicator mode",
            detail=detail,
        )

    def observational_price_action(df_entry, direction, cfg_obj):
        score, detail = original_price_action(df_entry, direction, cfg_obj)
        enriched = dict(detail) if isinstance(detail, dict) else {"detail": str(detail)}
        enriched.update({
            "observational_only": True,
            "original_score": score,
            "paper_two_indicator_bypass": True,
        })
        return 0.0, enriched

    watchlist_filters.classify_direction_eligibility = observational_classify
    rvol.passes_rvol_threshold = observational_rvol
    entry_quality.assess_entry_quality = observational_quality
    entry_confirmation.assess_entry_context = observational_context
    relative_strength.assess_relative_strength = observational_relative_strength
    price_action.evaluate_price_action = observational_price_action

    # main.py imports these names at module import time. These module patches
    # therefore become the functions main.py receives when runpy loads it.
    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = False
    cfg.ENABLE_NEWS_FILTER = False
    cfg.ENABLE_RVOL_FILTER = False

    logger.warning(
        "PAPER LEGACY GATES OBSERVATIONAL: EMA200, RVOL, entry quality, CHoCH/context, "
        "price action, relative strength, market alignment and news cannot block/re-rank entries"
    )


def install_two_indicator_patch():
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: EMA/RSI launcher requires PAPER_TRADING=True")

    install_legacy_gate_bypass()
    _config_snapshot()

    def evaluate(symbol, df_15m, df_5m, df_index_15m, cfg_obj):
        event = {
            "event": "ENTRY_EVALUATION",
            "logged_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "paper_only": True,
            "directional_policy": {
                "ema9_gt_ema21": "SELL",
                "ema9_lt_ema21": "BUY",
                "rsi_gte_70": "BUY_OVERRIDE",
                "rsi_lte_30": "SELL_OVERRIDE",
                "rsi_30_70": "PASS_EMA",
            },
            "legacy_gates": "OBSERVATIONAL_ONLY",
            "observations": _legacy_observations(df_15m, df_5m, df_index_15m),
        }
        if df_5m is None or df_5m.empty:
            event.update({"decision": "REJECT", "stage": "ENTRY_DATA", "reasons": ["NO_ENTRY_CANDLES"]})
            _append(event)
            return None

        cur = df_5m.iloc[-1]
        event["candle_time"] = _safe(cur.get("date"))
        if "date" not in cur or not _within_entry_window(cur["date"]):
            event.update({
                "decision": "REJECT",
                "stage": "TIME_WINDOW",
                "reasons": ["OUTSIDE_ENTRY_WINDOW"],
                "no_entry_before": getattr(cfg_obj, "NO_ENTRY_BEFORE", None),
                "no_entry_after": getattr(cfg_obj, "NO_ENTRY_AFTER", None),
            })
            _append(event)
            return None

        base, e9, e21 = ema_direction(df_5m)
        rsi = calculate_rsi(df_5m)
        override = rsi_direction(rsi)
        final = override or base
        event.update({
            "ema9": e9,
            "ema21": e21,
            "ema_gap": None if e9 is None or e21 is None else e9 - e21,
            "ema_base_direction": base,
            "rsi14": rsi,
            "rsi_override": override,
            "final_direction": final,
        })
        if base is None:
            event.update({"decision": "REJECT", "stage": "EMA_DIRECTION", "reasons": ["EMA9_EMA21_UNAVAILABLE_OR_EQUAL"]})
            _append(event)
            return None

        entry = float(cur["close"]) if not pd.isna(cur.get("close")) else 0.0
        if entry <= 0:
            event.update({"decision": "REJECT", "stage": "ENTRY_PRICE", "reasons": ["INVALID_ENTRY_PRICE"]})
            _append(event)
            return None

        sp = float(getattr(cfg_obj, "STOP_LOSS_PERCENT", .45)) / 100
        tp = float(getattr(cfg_obj, "PROFIT_TARGET_PERCENT", .70)) / 100
        if final == "BUY":
            stop = entry * (1 - sp)
            target = entry * (1 + tp)
        else:
            stop = entry * (1 + sp)
            target = entry * (1 - tp)

        event.update({
            "decision": "SIGNAL_SELECTED",
            "stage": "TWO_INDICATOR_SIGNAL",
            "entry_price": entry,
            "stop_loss": stop,
            "target": target,
            "stop_loss_percent": sp * 100,
            "profit_target_percent": tp * 100,
            "selection_reasons": ["EMA9_EMA21_DIRECTION_AVAILABLE", ("RSI_OVERRIDE" if override else "RSI_PASS")],
            "observational_filters_blocked": False,
        })
        _append(event)
        logger.info(
            "PAPER AUDIT SIGNAL | %s | EMA9=%s EMA21=%s base=%s RSI=%s override=%s FINAL=%s",
            symbol, e9, e21, base, rsi, override, final,
        )
        reason = (
            f"PAPER TWO-INDICATOR | REVERSED EMA9={e9:.4f} EMA21={e21:.4f} -> {base} | "
            f"RSI({RSI_PERIOD})={'NA' if rsi is None else f'{rsi:.2f}'} "
            f"{'RSI_OVERRIDE' if override else 'RSI_PASS'} -> {final} | "
            f"legacy technical gates observational only | audit={ENTRY_AUDIT}"
        )
        return strategy.Signal(
            symbol=symbol,
            direction=final,
            entry_price=entry,
            stop_loss=stop,
            target=target,
            timestamp=cur["date"],
            reason=reason,
            confidence=None,
        )

    strategy.evaluate = evaluate
    logger.warning(
        "PAPER TWO-INDICATOR AUDIT MODE ACTIVE: every entry evaluation and available legacy metric is persisted to %s",
        ENTRY_AUDIT,
    )


def main():
    install_two_indicator_patch()
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
