#!/usr/bin/env python3
"""Counterfactual replay of current LIVE entry filters over recorded trades.

This is research-only. It never imports or mutates live trading state.

It matches each recorded trade to an executed signal log record on the same date,
using symbol/direction/entry-price proximity, reconstructs current technical gates
from downloaded 3-minute Parquet candles, and computes counterfactual P&L where
rejected trades contribute zero and accepted trades retain their historical P&L.

Two scenarios are reported:
  CORE   = EMA9/21 direction + ADX>=20 + VWAP direction + EMA distance<=2 ATR
           + hard breakout validation (20-bar structure, volume>=1.5x,
             TR/ATR>=1.2x, CLV >= +0.60 BUY / <= -0.60 SELL).
  STRICT = CORE + stored confirmation_count>=2 when available + independent
           pullback/breakout evidence when available + cost coverage proxy
           (ATR * qty >= 1.7 * historical recorded costs).

STRICT is an approximation because historical logs do not contain every modern
pattern/rejection field. The script says so explicitly in its output.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

TRADE_PATH = Path("trade_history.jsonl")
SIGNAL_DIR = Path("signal_logs")
CANDLE_DIR = Path("runtime/trade_replay_history/candles_3minute")
OUT = Path("runtime/current_entry_replay_253")
OUT.mkdir(parents=True, exist_ok=True)

ADX_MIN = 20.0
EMA_DISTANCE_MAX_ATR = 2.0
BREAKOUT_LOOKBACK = 20
BREAKOUT_VOLUME_MIN = 1.5
BREAKOUT_ATR_MIN = 1.2
BUY_CLV_MIN = 0.60
SELL_CLV_MAX = -0.60
SECONDARY_REQUIRED = 2
COST_COVERAGE_MULT = 1.7


def load_jsonl(path: Path):
    out = []
    if not path.exists():
        return out
    for line in path.open(errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_trades():
    return load_jsonl(TRADE_PATH)


def load_signals_by_date():
    result = {}
    for p in sorted(SIGNAL_DIR.glob("signals_*.jsonl")):
        d = p.stem.replace("signals_", "")
        result[d] = load_jsonl(p)
    return result


def fnum(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def boolish(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes", "y", "pass", "passed", "bullish", "bearish"}
    return False


def signal_timestamp(s):
    for k in ("signal_candle_close", "timestamp", "order_submitted_at", "scan_started_at"):
        if s.get(k):
            ts = pd.to_datetime(s[k], errors="coerce")
            if not pd.isna(ts):
                return ts
    return pd.NaT


def signal_entry(s):
    return fnum(s.get("confirmed_entry_price"), fnum(s.get("entry_price")))


def match_trade_to_signal(trade, signals):
    symbol = str(trade.get("symbol") or "")
    direction = str(trade.get("direction") or "").upper()
    entry = fnum(trade.get("entry"))
    candidates = []
    for s in signals:
        if str(s.get("symbol") or "") != symbol:
            continue
        if str(s.get("direction") or "").upper() != direction:
            continue
        se = signal_entry(s)
        if se is None or entry is None:
            continue
        # Prefer executed=true but tolerate legacy logs where execution flag was absent.
        executed_penalty = 0 if s.get("executed") is True else 1
        rel = abs(se - entry) / max(abs(entry), 1e-9)
        candidates.append((executed_penalty, rel, abs(se-entry), s))
    if not candidates:
        return None, "NO_SIGNAL_MATCH"
    candidates.sort(key=lambda z: (z[0], z[1], z[2]))
    best = candidates[0]
    # 1% price mismatch is already suspicious; keep record but call it weak.
    quality = "EXACTISH" if best[1] <= 0.0025 else ("WEAK" if best[1] <= 0.01 else "POOR")
    return best[3], quality


def load_candles(symbol):
    p = CANDLE_DIR / f"{symbol.replace('/', '_')}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def add_indicators(df):
    x = df.copy()
    for c in ("open", "high", "low", "close", "volume"):
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x["ema9"] = x["close"].ewm(span=9, adjust=False).mean()
    x["ema21"] = x["close"].ewm(span=21, adjust=False).mean()
    x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()

    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["tr"] = tr
    x["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()

    up = x["high"].diff()
    down = -x["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=x.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=x.index)
    atr_w = tr.ewm(alpha=1/14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x["adx14"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # Intraday VWAP resets each calendar day.
    typical = (x["high"] + x["low"] + x["close"]) / 3.0
    x["pv"] = typical * x["volume"].fillna(0)
    local_date = x["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date if x["timestamp"].dt.tz is not None else x["timestamp"].dt.date
    x["vwap"] = x["pv"].groupby(local_date).cumsum() / x["volume"].fillna(0).groupby(local_date).cumsum().replace(0, np.nan)

    x["prior20_high"] = x["high"].shift(1).rolling(BREAKOUT_LOOKBACK).max()
    x["prior20_low"] = x["low"].shift(1).rolling(BREAKOUT_LOOKBACK).min()
    x["prior20_vol"] = x["volume"].shift(1).rolling(BREAKOUT_LOOKBACK).mean()
    x["volume_ratio20"] = x["volume"] / x["prior20_vol"].replace(0, np.nan)
    x["atr_multiple"] = x["tr"] / x["atr14"].replace(0, np.nan)
    rng = x["high"] - x["low"]
    x["clv"] = np.where(rng > 0, ((x["close"]-x["low"]) - (x["high"]-x["close"])) / rng, 0.0)
    x["ema_distance_atr"] = (x["close"] - x["ema9"]).abs() / x["atr14"].replace(0, np.nan)
    return x


def row_at_or_before(df, ts, trade_date):
    if ts is pd.NaT or pd.isna(ts):
        return None
    # Standardize timestamp timezone to candle timezone when possible.
    candle_tz = df["timestamp"].dt.tz
    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        else:
            ts = ts.tz_convert(candle_tz)
    subset = df[df["timestamp"] <= ts]
    if trade_date:
        d = pd.to_datetime(trade_date).date()
        dates = subset["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date if subset["timestamp"].dt.tz is not None else subset["timestamp"].dt.date
        subset = subset[dates == d]
    if subset.empty:
        return None
    return subset.iloc[-1]


def evaluate_gates(row, direction, signal, trade):
    direction = direction.upper()
    buy = direction == "BUY"
    close = fnum(row.get("close"))
    ema9 = fnum(row.get("ema9"))
    ema21 = fnum(row.get("ema21"))
    adx = fnum(row.get("adx14"))
    vwap = fnum(row.get("vwap"))
    eda = fnum(row.get("ema_distance_atr"))
    vr = fnum(row.get("volume_ratio20"))
    am = fnum(row.get("atr_multiple"))
    clv = fnum(row.get("clv"))
    p20h = fnum(row.get("prior20_high"))
    p20l = fnum(row.get("prior20_low"))

    direction_pass = None not in (ema9, ema21) and ((ema9 > ema21) if buy else (ema9 < ema21))
    adx_pass = adx is not None and adx >= ADX_MIN
    vwap_pass = None not in (close, vwap) and ((close >= vwap) if buy else (close <= vwap))
    ema_distance_pass = eda is not None and eda <= EMA_DISTANCE_MAX_ATR
    structure_pass = None not in (close, p20h, p20l) and ((close > p20h) if buy else (close < p20l))
    volume_pass = vr is not None and vr >= BREAKOUT_VOLUME_MIN
    atr_pass = am is not None and am >= BREAKOUT_ATR_MIN
    clv_pass = clv is not None and ((clv >= BUY_CLV_MIN) if buy else (clv <= SELL_CLV_MAX))
    breakout_pass = structure_pass and volume_pass and atr_pass and clv_pass
    core_pass = direction_pass and adx_pass and vwap_pass and ema_distance_pass and breakout_pass

    conf = signal.get("confirmation_count")
    if conf is None and isinstance(signal.get("entry_context_detail"), dict):
        conf = signal["entry_context_detail"].get("confirmation_count")
    conf_num = fnum(conf)
    confirmation_available = conf_num is not None
    confirmation_pass = (conf_num >= SECONDARY_REQUIRED) if confirmation_available else None

    pullback = signal.get("pullback")
    breakout_logged = signal.get("breakout")
    if isinstance(signal.get("entry_context_detail"), dict):
        ec = signal["entry_context_detail"]
        if pullback is None:
            pullback = ec.get("pullback")
        if breakout_logged is None:
            breakout_logged = ec.get("breakout")
    independent_available = pullback is not None or breakout_logged is not None
    independent_pass = (boolish(pullback) or boolish(breakout_logged)) if independent_available else None

    qty = fnum(trade.get("qty"), 0.0) or 0.0
    costs = fnum(trade.get("costs"), 0.0) or 0.0
    atr = fnum(row.get("atr14"))
    expected_gross = (atr or 0.0) * qty
    cost_pass = expected_gross >= COST_COVERAGE_MULT * costs if qty > 0 and atr is not None else None

    # STRICT only asserts historical extras when those fields exist; missing fields are
    # kept UNKNOWN rather than silently treated as failures. This avoids punishing old
    # records solely because logging was poorer.
    strict_pass = core_pass
    if confirmation_available:
        strict_pass = strict_pass and confirmation_pass
    if independent_available:
        strict_pass = strict_pass and independent_pass
    if cost_pass is not None:
        strict_pass = strict_pass and cost_pass

    fails = []
    for name, ok in [
        ("EMA_DIRECTION", direction_pass), ("ADX", adx_pass), ("VWAP", vwap_pass),
        ("EMA_DISTANCE", ema_distance_pass), ("STRUCTURE", structure_pass),
        ("BREAKOUT_VOLUME", volume_pass), ("BREAKOUT_ATR", atr_pass), ("CLV", clv_pass),
    ]:
        if ok is False:
            fails.append(name)

    return {
        "core_pass": bool(core_pass),
        "strict_pass": bool(strict_pass),
        "direction_pass": direction_pass,
        "adx_pass": adx_pass,
        "vwap_pass": vwap_pass,
        "ema_distance_pass": ema_distance_pass,
        "structure_pass": structure_pass,
        "volume_pass": volume_pass,
        "atr_pass": atr_pass,
        "clv_pass": clv_pass,
        "confirmation_available": confirmation_available,
        "confirmation_count": conf_num,
        "confirmation_pass": confirmation_pass,
        "independent_available": independent_available,
        "independent_pass": independent_pass,
        "cost_pass": cost_pass,
        "failed_core_components": ";".join(fails),
        "close": close, "ema9": ema9, "ema21": ema21, "adx14": adx,
        "vwap": vwap, "ema_distance_atr": eda, "volume_ratio20": vr,
        "atr_multiple": am, "clv": clv, "atr14": atr,
        "expected_gross_proxy": expected_gross,
    }


def main():
    trades = load_trades()
    signals_by_date = load_signals_by_date()
    candle_cache = {}
    rows = []

    for i, trade in enumerate(trades, 1):
        d = str(trade.get("date") or "")
        symbol = str(trade.get("symbol") or "")
        direction = str(trade.get("direction") or "").upper()
        actual_net = fnum(trade.get("pnl"), 0.0) or 0.0
        actual_gross = fnum(trade.get("gross_pnl"), actual_net) or 0.0
        costs = fnum(trade.get("costs"), 0.0) or 0.0
        base = {
            "trade_index": i, "date": d, "symbol": symbol, "direction": direction,
            "entry": fnum(trade.get("entry")), "historical_exit": fnum(trade.get("exit")),
            "qty": fnum(trade.get("qty"), 0.0), "actual_result": trade.get("result"),
            "actual_net_pnl": actual_net, "actual_gross_pnl": actual_gross, "historical_costs": costs,
        }

        path = CANDLE_DIR / f"{symbol.replace('/', '_')}.parquet"
        if not path.exists():
            rows.append({**base, "replay_status": "NO_CANDLES", "core_pass": None, "strict_pass": None})
            continue
        if symbol not in candle_cache:
            candle_cache[symbol] = add_indicators(load_candles(symbol))

        signal, match_quality = match_trade_to_signal(trade, signals_by_date.get(d, []))
        if signal is None:
            rows.append({**base, "replay_status": "NO_SIGNAL_MATCH", "signal_match_quality": match_quality,
                         "core_pass": None, "strict_pass": None})
            continue

        ts = signal_timestamp(signal)
        row = row_at_or_before(candle_cache[symbol], ts, d)
        if row is None:
            rows.append({**base, "replay_status": "NO_CANDLE_AT_SIGNAL", "signal_match_quality": match_quality,
                         "signal_timestamp": str(ts), "core_pass": None, "strict_pass": None})
            continue

        gates = evaluate_gates(row, direction, signal, trade)
        rows.append({**base, "replay_status": "OK", "signal_match_quality": match_quality,
                     "signal_timestamp": str(ts), "matched_signal_entry": signal_entry(signal), **gates})

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "trade_level.csv", index=False)

    ok = df[df["replay_status"] == "OK"].copy()
    actual_total = float(df["actual_net_pnl"].sum())
    covered_actual = float(ok["actual_net_pnl"].sum()) if not ok.empty else 0.0
    core_net = float(ok.loc[ok["core_pass"] == True, "actual_net_pnl"].sum()) if not ok.empty else 0.0
    strict_net = float(ok.loc[ok["strict_pass"] == True, "actual_net_pnl"].sum()) if not ok.empty else 0.0

    gate_rejects = Counter()
    if not ok.empty:
        for txt in ok.loc[ok["core_pass"] == False, "failed_core_components"].fillna(""):
            for part in str(txt).split(";"):
                if part:
                    gate_rejects[part] += 1

    summary = {
        "historical_trade_count": int(len(df)),
        "historical_actual_net_pnl_all": actual_total,
        "replayable_trade_count": int(len(ok)),
        "replayable_actual_net_pnl": covered_actual,
        "replay_status_counts": df["replay_status"].value_counts(dropna=False).to_dict(),
        "signal_match_quality_counts": df.get("signal_match_quality", pd.Series(dtype=str)).value_counts(dropna=False).to_dict(),
        "core_accepted": int((ok["core_pass"] == True).sum()) if not ok.empty else 0,
        "core_rejected": int((ok["core_pass"] == False).sum()) if not ok.empty else 0,
        "core_counterfactual_net_pnl_using_historical_exits": core_net,
        "core_delta_vs_replayable_actual": core_net - covered_actual,
        "strict_accepted": int((ok["strict_pass"] == True).sum()) if not ok.empty else 0,
        "strict_rejected": int((ok["strict_pass"] == False).sum()) if not ok.empty else 0,
        "strict_counterfactual_net_pnl_using_historical_exits": strict_net,
        "strict_delta_vs_replayable_actual": strict_net - covered_actual,
        "core_rejection_component_counts": dict(gate_rejects),
        "thresholds": {
            "adx_min": ADX_MIN, "ema_distance_max_atr": EMA_DISTANCE_MAX_ATR,
            "breakout_lookback": BREAKOUT_LOOKBACK, "breakout_volume_min": BREAKOUT_VOLUME_MIN,
            "breakout_atr_min": BREAKOUT_ATR_MIN, "buy_clv_min": BUY_CLV_MIN,
            "sell_clv_max": SELL_CLV_MAX, "secondary_required": SECONDARY_REQUIRED,
            "cost_coverage_mult": COST_COVERAGE_MULT,
        },
        "important_limitation": "This isolates current entry filtering while retaining historical exits/P&L. STRICT uses logged historical confirmation/pullback-breakout fields where available; it is not yet a parity replay of the modern exit engine or every modern pattern/rejection field.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # Per-date comparison.
    if not ok.empty:
        daily = []
        for d, g in ok.groupby("date"):
            daily.append({
                "date": d,
                "replayable_trades": len(g),
                "actual_net": g["actual_net_pnl"].sum(),
                "core_accepted": int((g["core_pass"] == True).sum()),
                "core_net": g.loc[g["core_pass"] == True, "actual_net_pnl"].sum(),
                "strict_accepted": int((g["strict_pass"] == True).sum()),
                "strict_net": g.loc[g["strict_pass"] == True, "actual_net_pnl"].sum(),
            })
        pd.DataFrame(daily).to_csv(OUT / "daily.csv", index=False)

    print("===== CURRENT ENTRY FILTER COUNTERFACTUAL =====")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {OUT / 'trade_level.csv'}")
    print(f"Wrote {OUT / 'daily.csv'}")
    print(f"Wrote {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
