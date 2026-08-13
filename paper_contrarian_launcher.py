#!/usr/bin/env python3
"""Paper-only clean EMA direction and candlestick eligibility launcher.

ADX measures strength only, EMA9/EMA21 always selects the normal direction,
RSI is observational, and a fail-closed TA-Lib/confluence gate must approve the
latest completed entry candle.  The core technical gates remain enforced.
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
from candle_eligibility import evaluate_candle_eligibility

logger = logging.getLogger("paper_contrarian_launcher")
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
ADX_MIN_STRENGTH = 20.0
AUDIT_DIR = Path(__file__).resolve().parent / "runtime" / "paper_audit"
ENTRY_AUDIT = AUDIT_DIR / "entry_audit.jsonl"
CONFIG_AUDIT = AUDIT_DIR / "session_config.json"
TRADE_HISTORY_PATH = Path(__file__).resolve().parent / "trade_history.jsonl"


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
    snap["PAPER_TWO_INDICATOR_LEGACY_GATES"] = "DISABLED"
    snap["PAPER_ADX_MIN_STRENGTH"] = ADX_MIN_STRENGTH
    snap["PAPER_ADX_ROLE"] = "STRENGTH_ONLY_NEVER_REVERSES_DIRECTION"
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_AUDIT.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")


def _adx_entry_blocked(adx):
    if adx is None:
        return True
    minimum = float(getattr(cfg, "PAPER_ADX_MIN_STRENGTH", ADX_MIN_STRENGTH))
    return float(adx) < minimum


def _trade_group_key(record):
    signal_id = record.get("signal_id")
    if signal_id:
        return f"signal:{signal_id}"
    return "fallback:{symbol}|{direction}|{entry}|{entry_time}".format(
        symbol=record.get("symbol"),
        direction=record.get("direction"),
        entry=record.get("entry"),
        entry_time=record.get("entry_time"),
    )


def _paper_entry_guard(symbol, now=None, log_path=None):
    """Enforce per-symbol daily count and post-loss cooldown in PAPER mode.

    Hybrid/partial exit rows belonging to the same signal are aggregated into
    one completed entry, so a scalp + runner does not consume two symbol slots.
    """

    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        return True, {"paper_only": True, "active": False}

    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata")
    if now.tzinfo is None:
        now = now.tz_localize("Asia/Kolkata")
    else:
        now = now.tz_convert("Asia/Kolkata")

    max_per_symbol = int(getattr(cfg, "PAPER_MAX_TRADES_PER_SYMBOL", 2))
    cooldown_minutes = float(
        getattr(cfg, "PAPER_LOSS_REENTRY_COOLDOWN_MINUTES", 30.0)
    )
    path = Path(log_path) if log_path is not None else TRADE_HISTORY_PATH
    today = now.strftime("%Y-%m-%d")

    groups = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if record.get("date") != today or record.get("symbol") != symbol:
                        continue

                    key = _trade_group_key(record)
                    group = groups.setdefault(key, {"pnl": 0.0, "last_exit": None})
                    group["pnl"] += float(record.get("pnl") or 0.0)

                    time_text = record.get("time")
                    if time_text:
                        try:
                            exit_ts = pd.Timestamp(f"{today} {time_text}", tz="Asia/Kolkata")
                            if group["last_exit"] is None or exit_ts > group["last_exit"]:
                                group["last_exit"] = exit_ts
                        except Exception:
                            pass
        except OSError as exc:
            # Fail open on an observational persistence read problem. The main
            # RiskManager/day limit still remains active.
            return True, {
                "paper_only": True,
                "active": True,
                "history_read_error": str(exc),
                "decision": "ALLOW_FAIL_OPEN",
            }

    trade_count = len(groups)
    detail = {
        "paper_only": True,
        "active": True,
        "symbol_trade_count": trade_count,
        "max_trades_per_symbol": max_per_symbol,
        "loss_cooldown_minutes": cooldown_minutes,
    }

    if max_per_symbol > 0 and trade_count >= max_per_symbol:
        detail.update({
            "decision": "BLOCK",
            "reason": "MAX_TRADES_PER_SYMBOL",
        })
        return False, detail

    completed = [g for g in groups.values() if g.get("last_exit") is not None]
    if completed:
        latest = max(completed, key=lambda g: g["last_exit"])
        latest_pnl = float(latest.get("pnl") or 0.0)
        elapsed_minutes = max(
            0.0,
            (now - latest["last_exit"]).total_seconds() / 60.0,
        )
        detail.update({
            "latest_completed_trade_pnl": latest_pnl,
            "minutes_since_latest_exit": elapsed_minutes,
        })
        if latest_pnl < 0 and elapsed_minutes < cooldown_minutes:
            detail.update({
                "decision": "BLOCK",
                "reason": "LOSS_REENTRY_COOLDOWN",
                "cooldown_remaining_minutes": cooldown_minutes - elapsed_minutes,
            })
            return False, detail

    detail["decision"] = "ALLOW"
    return True, detail


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


def _latest_adx(df_15m):
    """Return latest available stock ADX for the paper regime/entry policy."""
    if df_15m is None or df_15m.empty or "adx" not in df_15m.columns:
        return None
    values = pd.to_numeric(df_15m["adx"], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.iloc[-1])


def ema_direction(df, adx=None):
    """Return normal EMA direction; ADX never reverses the signal."""
    if df is None or df.empty or "close" not in df.columns or len(df) < EMA_SLOW:
        return None, None, None
    c = pd.to_numeric(df["close"], errors="coerce")
    e9 = c.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    e21 = c.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    if pd.isna(e9) or pd.isna(e21):
        return None, None, None

    if e9 > e21:
        direction = "BUY"
    elif e9 < e21:
        direction = "SELL"
    else:
        direction = None
    return direction, float(e9), float(e21)


def rsi_direction(rsi):
    """RSI is retained for audit only and can never override direction."""
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
            "adx_min_strength": ADX_MIN_STRENGTH,
            "adx_role": "STRENGTH_ONLY",
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


def install_two_indicator_patch():
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: EMA/RSI launcher requires PAPER_TRADING=True")

    cfg.PAPER_ADX_MIN_STRENGTH = float(getattr(cfg, "PAPER_ADX_MIN_STRENGTH", 20.0))
    cfg.PAPER_BUY_MIN_ADX = float(getattr(cfg, "PAPER_BUY_MIN_ADX", 25.0))
    cfg.PAPER_SELL_MIN_ADX = float(getattr(cfg, "PAPER_SELL_MIN_ADX", 20.0))
    cfg.PAPER_CANDLE_MIN_VOLUME_RATIO = float(getattr(cfg, "PAPER_CANDLE_MIN_VOLUME_RATIO", 1.5))
    cfg.PAPER_CANDLE_MAX_FRESH_SECONDS = float(getattr(cfg, "PAPER_CANDLE_MAX_FRESH_SECONDS", 90.0))
    cfg.PAPER_CANDLE_COMPLETION_GRACE_SECONDS = float(getattr(cfg, "PAPER_CANDLE_COMPLETION_GRACE_SECONDS", 5.0))
    cfg.ENABLE_RVOL_FILTER = True
    cfg.RVOL_THRESHOLD = max(float(getattr(cfg, "RVOL_THRESHOLD", 1.5)), 1.5)
    cfg.ENABLE_200_EMA_FILTER = True
    cfg.ENABLE_EMA200_WATCHLIST = True
    cfg.ENABLE_ENTRY_TIMING_FILTER = True
    cfg.ENABLE_CONFIRMATION_QUALITY_FILTER = True
    cfg.ENABLE_VOLUME_ACCELERATION_FILTER = True
    cfg.ENABLE_PRICE_ACTION = True
    cfg.PAPER_PRICE_ACTION_OBSERVATIONAL = False
    _config_snapshot()

    def evaluate(symbol, df_15m, df_5m, df_index_15m, cfg_obj):
        observations = _legacy_observations(df_15m, df_5m, df_index_15m)
        adx = _latest_adx(df_15m)
        adx_regime = "STRENGTH_PASS" if not _adx_entry_blocked(adx) else "STRENGTH_REJECT"
        adx_blocked = _adx_entry_blocked(adx)
        event = {
            "event": "ENTRY_EVALUATION",
            "logged_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "paper_only": True,
            "directional_policy": {
                "adx_unavailable_or_lt_20": "BLOCK_ENTRY",
                "adx_gte_20": "STRENGTH_ONLY",
                "normal_ema9_gt_ema21": "BUY",
                "normal_ema9_lt_ema21": "SELL",
                "rsi14": "OBSERVATIONAL_ONLY",
            },
            "adx": adx,
            "adx_threshold": ADX_MIN_STRENGTH,
            "adx_regime": adx_regime,
            "adx_blocks_trade": adx_blocked,
            "legacy_gates": "ENFORCED",
            "observations": observations,
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

        timeframe_text = str(getattr(cfg_obj, "ENTRY_TIMEFRAME", "3minute"))
        timeframe_digits = "".join(ch for ch in timeframe_text if ch.isdigit())
        decision_time = pd.Timestamp(cur["date"]) + pd.Timedelta(
            minutes=int(timeframe_digits or 3)
        )
        completed_trend = strategy.latest_completed_15m_row(df_15m, decision_time)
        if completed_trend is None:
            event.update({
                "decision": "REJECT",
                "stage": "COMPLETED_15M_CONTEXT",
                "reasons": ["NO_COMPLETED_15M_CANDLE"],
            })
            _append(event)
            return None

        completed_df_15m = completed_trend.to_frame().T
        adx = _latest_adx(completed_df_15m)
        adx_blocked = _adx_entry_blocked(adx)
        adx_regime = "STRENGTH_PASS" if not adx_blocked else "STRENGTH_REJECT"
        event.update({
            "adx": adx,
            "adx_regime": adx_regime,
            "adx_blocks_trade": adx_blocked,
            "completed_15m_candle": _row_snapshot(completed_trend),
        })

        if adx_blocked:
            event.update({
                "decision": "REJECT",
                "stage": "ADX_STRENGTH_GATE",
                "reasons": ["ADX_BELOW_MINIMUM_OR_UNAVAILABLE"],
                "adx_minimum": getattr(cfg, "PAPER_ADX_MIN_STRENGTH", 20.0),
            })
            _append(event)
            logger.info(
                "PAPER ENTRY BLOCK | %s | ADX=%s below/unavailable minimum %.0f",
                symbol,
                adx,
                float(getattr(cfg, "PAPER_ADX_MIN_STRENGTH", 20.0)),
            )
            return None

        guard_ok, guard_detail = _paper_entry_guard(symbol)
        event["paper_entry_guard"] = guard_detail
        if not guard_ok:
            event.update({
                "decision": "REJECT",
                "stage": "PAPER_SYMBOL_RISK_GUARD",
                "reasons": [guard_detail.get("reason", "PAPER_SYMBOL_RISK_GUARD")],
            })
            _append(event)
            logger.info(
                "PAPER ENTRY BLOCK | %s | %s",
                symbol,
                guard_detail,
            )
            return None

        base, e9, e21 = ema_direction(df_5m, adx=adx)
        rsi = calculate_rsi(df_5m)
        override = None
        final = base
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

        candle_result = evaluate_candle_eligibility(
            df_5m, completed_df_15m, final, cfg_obj
        )
        event["candle_eligibility"] = candle_result.to_dict()
        if not candle_result.accepted:
            event.update({
                "decision": "REJECT",
                "stage": "CANDLE_ELIGIBILITY",
                "reasons": candle_result.reasons,
            })
            _append(event)
            logger.info(
                "PAPER CANDLE BLOCK | %s | direction=%s | reasons=%s",
                symbol, final, candle_result.reasons,
            )
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
            "stage": "CLEAN_CANDLE_ELIGIBILITY_SIGNAL",
            "entry_price": entry,
            "stop_loss": stop,
            "target": target,
            "stop_loss_percent": sp * 100,
            "profit_target_percent": tp * 100,
            "selection_reasons": [
                "NORMAL_EMA9_EMA21_DIRECTION",
                "ADX_STRENGTH_PASS",
                "RSI_OBSERVATIONAL",
                "TALIB_CANDLE_CONFLUENCE_PASS",
            ],
            "legacy_filters": "ENFORCED",
        })
        _append(event)
        logger.info(
            "PAPER CLEAN SIGNAL | %s | ADX=%s | EMA9=%s EMA21=%s direction=%s RSI(obs)=%s",
            symbol, adx, e9, e21, final, rsi,
        )
        reason = (
            f"PAPER CLEAN CANDLE | ADX={adx:.2f} strength-only | "
            f"EMA9={e9:.4f} EMA21={e21:.4f} -> {final} | "
            f"RSI({RSI_PERIOD})={'NA' if rsi is None else f'{rsi:.2f}'} observational | "
            f"TA-Lib pattern + volume + VWAP + EMA200 passed | core gates enforced | audit={ENTRY_AUDIT}"
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
        "PAPER CLEAN ENTRY ACTIVE: normal EMA direction; ADX strength only (minimum %.0f); "
        "RSI observational; TA-Lib candle confluence fail-closed",
        ADX_MIN_STRENGTH,
    )
    logger.warning(
        "PAPER SYMBOL GUARD ACTIVE: max %s completed entries/symbol/day; %.0f-minute cooldown after losing trade",
        int(getattr(cfg, "PAPER_MAX_TRADES_PER_SYMBOL", 2)),
        float(getattr(cfg, "PAPER_LOSS_REENTRY_COOLDOWN_MINUTES", 30.0)),
    )
    logger.warning(
        "PAPER AUDIT MODE ACTIVE: every entry evaluation and available legacy metric is persisted to %s",
        ENTRY_AUDIT,
    )


def main():
    install_two_indicator_patch()
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
