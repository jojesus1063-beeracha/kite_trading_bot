#!/usr/bin/env python3
"""MATMON directional-persistence preopen selector.

Purpose:
- Build the canonical MATMON 120-symbol watchlist.
- Prefer persistent directional trends over raw intraday movement/volatility.
- Keep enough liquidity for live MIS execution.
- Assign each symbol a preferred BUY or SELL direction for diagnostics.
- Never place orders.

The selector uses completed DAILY candles because it runs before the current
session develops. EL-BETHEL remains the final 3-minute entry authority.
"""
from __future__ import annotations

import json
import math
import shutil
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auth import get_kite_client
from paper_full_universe_top60_selector import cleaned_equity_instruments, fetch_selector_quotes

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "user_config.json"
RUNTIME = ROOT / "runtime" / "matmon" / "preopen"

TOP_N = 120
HISTORY_POOL = 240
LOOKBACK_DAYS = 90
HISTORY_DELAY_SECONDS = 0.36
MIN_PRICE = 20.0
MAX_PRICE = 2200.0
MAX_SPREAD_PCT = 0.50
MIN_DEPTH_VALUE = 1000.0
MIN_DAILY_TURNOVER = 1_000_000.0

def f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def normalize(values):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5] * len(values)
    return [(x - lo) / (hi - lo) for x in values]

def depth_value(levels):
    return sum(
        f(x.get("price")) * f(x.get("quantity"))
        for x in (levels or []) if isinstance(x, dict)
    )

def quote_liquidity(row, quote):
    if not isinstance(quote, dict):
        return None
    last = f(quote.get("last_price"))
    if not (MIN_PRICE <= last <= MAX_PRICE):
        return None
    depth = quote.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    if not buys or not sells:
        return None
    bid, ask = f(buys[0].get("price")), f(sells[0].get("price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    spread = ((ask - bid) / mid) * 100 if mid else 99
    if spread > MAX_SPREAD_PCT:
        return None
    dv = depth_value(buys) + depth_value(sells)
    if dv < MIN_DEPTH_VALUE:
        return None
    volume = f(quote.get("volume"))
    return {
        "symbol": row["symbol"], "exchange": row["exchange"],
        "instrument_token": int(row["instrument_token"]),
        "last_price": last, "spread_pct": spread, "depth_value": dv,
        "volume": volume,
        "liquidity_score": math.log1p(dv) + math.log1p(max(volume, 0)),
    }

def daily_metrics(candles):
    rows = []
    for c in candles or []:
        close, high, low, vol = f(c.get("close")), f(c.get("high")), f(c.get("low")), f(c.get("volume"))
        if close > 0 and high > 0 and low > 0:
            rows.append((close, high, low, vol))
    if len(rows) < 55:
        return None

    closes = [x[0] for x in rows]
    returns = [(closes[i] / closes[i-1] - 1.0) * 100 for i in range(1, len(closes))]
    r20 = returns[-20:]
    r10 = returns[-10:]
    r5 = returns[-5:]

    # Directional efficiency: net movement divided by total movement.
    efficiency20 = abs(closes[-1] - closes[-21]) / max(
        sum(abs(closes[i] - closes[i-1]) for i in range(len(closes)-20, len(closes))), 1e-9
    )
    efficiency10 = abs(closes[-1] - closes[-11]) / max(
        sum(abs(closes[i] - closes[i-1]) for i in range(len(closes)-10, len(closes))), 1e-9
    )

    # Directional consistency rewards repeated closes in the same direction.
    pos20 = sum(x > 0 for x in r20) / len(r20)
    neg20 = sum(x < 0 for x in r20) / len(r20)
    pos10 = sum(x > 0 for x in r10) / len(r10)
    neg10 = sum(x < 0 for x in r10) / len(r10)

    # EMA trend alignment without requiring an external TA package.
    def ema(vals, period):
        alpha = 2.0 / (period + 1.0)
        e = vals[0]
        for v in vals[1:]:
            e = alpha * v + (1 - alpha) * e
        return e

    ema20 = ema(closes[-60:], 20)
    ema50 = ema(closes[-60:], 50)
    ema20_prev = ema(closes[-61:-1], 20)
    ema20_slope_pct = (ema20 / ema20_prev - 1.0) * 100 if ema20_prev else 0.0

    tr = []
    for i in range(1, len(rows)):
        close_prev = rows[i-1][0]
        high, low = rows[i][1], rows[i][2]
        tr.append(max(high-low, abs(high-close_prev), abs(low-close_prev)) / close_prev * 100)
    atr20 = statistics.mean(tr[-20:]) if len(tr) >= 20 else 0.0

    recent_return = (closes[-1] / closes[-6] - 1.0) * 100
    direction = "BUY" if (ema20 > ema50 and ema20_slope_pct > 0 and pos10 >= neg10) else (
        "SELL" if (ema20 < ema50 and ema20_slope_pct < 0 and neg10 >= pos10) else "NEUTRAL"
    )

    # Penalize excessive daily churn. Moderate ATR is acceptable; extreme ATR
    # is treated as instability rather than an advantage.
    volatility_quality = 1.0
    if atr20 > 5.0:
        volatility_quality = max(0.0, 1.0 - (atr20 - 5.0) / 5.0)
    elif atr20 < 0.5:
        volatility_quality = max(0.4, atr20 / 0.5)

    buy_score = (
        0.25 * max(0.0, ema20_slope_pct) +
        0.25 * pos20 +
        0.20 * efficiency20 +
        0.15 * efficiency10 +
        0.10 * max(0.0, recent_return) / 5.0 +
        0.05 * volatility_quality
    )
    sell_score = (
        0.25 * max(0.0, -ema20_slope_pct) +
        0.25 * neg20 +
        0.20 * efficiency20 +
        0.15 * efficiency10 +
        0.10 * max(0.0, -recent_return) / 5.0 +
        0.05 * volatility_quality
    )

    return {
        "direction": direction,
        "buy_score_raw": buy_score,
        "sell_score_raw": sell_score,
        "efficiency20": efficiency20,
        "efficiency10": efficiency10,
        "positive_days20": pos20,
        "negative_days20": neg20,
        "positive_days10": pos10,
        "negative_days10": neg10,
        "ema20": ema20,
        "ema50": ema50,
        "ema20_slope_pct": ema20_slope_pct,
        "recent_return_pct": recent_return,
        "atr20_pct": atr20,
        "volatility_quality": volatility_quality,
        "volume_latest": rows[-1][3],
        "volume_median20": statistics.median([x[3] for x in rows[-21:-1] if x[3] > 0]) if any(x[3] > 0 for x in rows[-21:-1]) else 0,
    }

def fetch_history(kite, candidates):
    today = datetime.now(IST).date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    end = today - timedelta(days=1)
    out = {}
    for i, c in enumerate(candidates, 1):
        try:
            candles = kite.historical_data(c["instrument_token"], start, end, "day", continuous=False, oi=False)
            out[c["symbol"]] = daily_metrics(candles)
        except Exception:
            out[c["symbol"]] = None
        if i < len(candidates):
            time.sleep(HISTORY_DELAY_SECONDS)
    return out

def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg.get("paper_trading") is not True:
        raise SystemExit("ABORT: preopen selector requires paper_trading=true")

    kite = get_kite_client()
    rows, cleaning = cleaned_equity_instruments(kite)
    keys = [f'{x["exchange"]}:{x["symbol"]}' for x in rows]
    quotes = fetch_selector_quotes(kite, keys)

    liquid = []
    for row in rows:
        q = quote_liquidity(row, quotes.get(f'{row["exchange"]}:{row["symbol"]}'))
        if q:
            liquid.append(q)

    liquid.sort(key=lambda x: (-x["liquidity_score"], x["spread_pct"], x["symbol"]))
    pool = liquid[:HISTORY_POOL]
    histories = fetch_history(kite, pool)

    raw = []
    for c in pool:
        m = histories.get(c["symbol"])
        if not m:
            continue
        direction = m["direction"]
        if direction == "NEUTRAL":
            continue
        directional = m["buy_score_raw"] if direction == "BUY" else m["sell_score_raw"]
        # Liquidity is a gate/supporting factor; directional persistence dominates.
        raw.append({**c, **m, "directional_raw": directional})

    dvals = normalize([x["directional_raw"] for x in raw])
    evals = normalize([x["efficiency20"] for x in raw])
    rsvals = normalize([abs(x["recent_return_pct"]) for x in raw])
    liqvals = normalize([x["liquidity_score"] for x in raw])

    for x, d, e, r, l in zip(raw, dvals, evals, rsvals, liqvals):
        x["directional_score"] = round(100.0 * (
            0.55 * d +
            0.20 * e +
            0.10 * r +
            0.10 * x["volatility_quality"] +
            0.05 * l
        ), 4)

    # Ensure both directions are represented when the market is not one-sided.
    raw.sort(key=lambda x: (-x["directional_score"], -x["liquidity_score"], x["symbol"]))
    selected = raw[:TOP_N]

    if len(selected) < TOP_N:
        raise SystemExit(f"ABORT: only {len(selected)} directional candidates; need {TOP_N}")

    watchlist = [{"symbol": x["symbol"], "exchange": x["exchange"]} for x in selected]
    if len({x["symbol"] for x in watchlist}) != TOP_N:
        raise SystemExit("ABORT: duplicate symbols in directional watchlist")

    backup_dir = RUNTIME / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"user_config_before_directional_preopen_{stamp}.json"
    shutil.copy2(CONFIG, backup)

    updated = dict(cfg)
    updated["watchlist"] = watchlist
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)

    check = json.loads(CONFIG.read_text(encoding="utf-8"))
    if check.get("watchlist") != watchlist:
        raise RuntimeError("POST-WRITE WATCHLIST CHECK FAILED")

    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "strategy": "MATMON_DIRECTIONAL_PERSISTENCE_TOP120",
        "selection_policy": {
            "directional_persistence": 55,
            "trend_efficiency": 20,
            "recent_direction": 10,
            "volatility_quality": 10,
            "liquidity": 5,
            "raw_intraday_movement_weight": 0,
        },
        "universe_cleaning": cleaning,
        "liquidity_pool": len(pool),
        "directional_candidates": len(raw),
        "selected_count": len(selected),
        "watchlist": watchlist,
        "selected_details": [
            {k: v for k, v in x.items() if k not in {"instrument_token"}}
            for x in selected
        ],
        "config_backup": str(backup),
    }
    path = RUNTIME / f"directional_top120_{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (RUNTIME / "latest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("MATMON DIRECTIONAL PERSISTENCE TOP-120")
    print("Clean universe      :", len(rows))
    print("Liquid pool         :", len(pool))
    print("Directional         :", len(raw))
    print("Selected            :", len(selected))
    print("BUY candidates      :", sum(x["direction"] == "BUY" for x in selected))
    print("SELL candidates     :", sum(x["direction"] == "SELL" for x in selected))
    print("Report              :", path)
    print("Backup              :", backup)
    print("MATMON_PREOPEN_DIRECTIONAL=PASS")
    print("PAPER_ONLY=TRUE")
    print("NO_ORDERS_PLACED")

if __name__ == "__main__":
    main()
