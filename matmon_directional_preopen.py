#!/usr/bin/env python3
"""Matmon directional-persistence pre-open selector.

Replaces volatility/liquidity-first ranking with multi-day directional
structure so the live shortlist prefers names that have been moving
cleanly in one direction.

Pipeline:
  1. Clean ordinary NSE/BSE equities (same universe cleaner as today).
  2. Pre-open quote gates: price (+ optional spread/depth when present).
  3. Liquidity pre-rank -> history pool (default 400) to keep runtime feasible.
  4. Daily history features:
       - trend alignment (close vs EMA20/EMA50)
       - directional consistency over N days
       - trend efficiency (anti-chop)
       - DI agreement + modest ADX floor
  5. Extreme ATR% tails penalized / rejected.
  6. Rank by directional score; write Top-N unique symbols.

Does NOT change Matmon entry logic (EMA3/15 -> DI14 -> CLEAN -> micro).
Default is shadow (report only). --write updates user_config.json.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token
from paper_full_universe_top60_selector import (
    cleaned_equity_instruments,
    fetch_selector_quotes,
)
import matmon_strategy_config as strategy_def

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "user_config.json"
RUNTIME = ROOT / "runtime" / "matmon" / "preopen_directional"

DEFAULT_TOP_N = 120
DEFAULT_HISTORY_POOL = 400
DEFAULT_LOOKBACK_DAYS = 12
DEFAULT_CONSISTENCY_BARS = 8

MIN_PRICE = 20.0
MAX_PRICE = 2200.0
MAX_SPREAD_PCT = 0.75  # slightly wider pre-open; tightened when depth exists
MIN_DEPTH_VALUE = 500.0  # soft preference, not hard reject when depth missing

W_ALIGN = 0.25
W_CONSISTENCY = 0.30
W_EFFICIENCY = 0.25
W_DI = 0.15
W_LIQ = 0.05

ATR_TAIL_MULTIPLIER = 2.5
MIN_ADX = 18.0


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def depth_side_value(levels):
    total = 0.0
    for x in levels or []:
        if not isinstance(x, dict):
            continue
        total += num(x.get("price")) * num(x.get("quantity"))
    return total


def quote_gate(row, quote):
    """Price gate required. Spread/depth optional (often empty pre-09:00)."""
    if not isinstance(quote, dict):
        return None

    # Prefer last_price; fall back to OHLC close (prev session) pre-open.
    last = num(quote.get("last_price"))
    if last <= 0:
        ohlc = quote.get("ohlc") or {}
        last = num(ohlc.get("close")) or num(ohlc.get("open"))
    if not (MIN_PRICE <= last <= MAX_PRICE):
        return None

    depth = quote.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    bid = ask = 0.0
    spread_pct = 0.0
    depth_value = 0.0
    has_book = bool(buys) and bool(sells)

    if has_book:
        bid = num(buys[0].get("price"))
        ask = num(sells[0].get("price"))
        if bid > 0 and ask > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            if mid > 0:
                spread_pct = ((ask - bid) / mid) * 100.0
                if spread_pct > MAX_SPREAD_PCT:
                    return None
            depth_value = depth_side_value(buys) + depth_side_value(sells)
        else:
            has_book = False

    volume = num(quote.get("volume"))
    # Liquidity proxy: depth if present, else log1p(volume) or price-based floor
    if depth_value > 0:
        liq_raw = math.log1p(depth_value)
    elif volume > 0:
        liq_raw = math.log1p(volume) * 0.5
    else:
        liq_raw = math.log1p(last)  # weak but non-zero so ranking still works

    return {
        "symbol": row["symbol"],
        "exchange": row["exchange"],
        "instrument_token": row.get("instrument_token"),
        "last_price": round(last, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread_pct": round(spread_pct, 6),
        "depth_value": round(depth_value, 2),
        "volume": int(volume),
        "has_book": has_book,
        "_liq_raw": liq_raw,
    }


def _ema_series(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def _wilder_di_adx(df: pd.DataFrame, period: int = 14):
    if df is None or len(df) < period + 2:
        return None, None, None

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    def last_finite(s):
        s = pd.to_numeric(s, errors="coerce").dropna()
        if s.empty:
            return None
        v = float(s.iloc[-1])
        return v if math.isfinite(v) else None

    return last_finite(plus_di), last_finite(minus_di), last_finite(adx)


def directional_features(df: pd.DataFrame, consistency_bars: int):
    if df is None or df.empty or len(df) < max(6, consistency_bars):
        return None

    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    valid = close.notna() & open_.notna()
    close = close[valid]
    open_ = open_[valid]
    high = high[valid]
    low = low[valid]
    if len(close) < max(6, consistency_bars):
        return None

    ema20 = _ema_series(close, 20)
    ema50 = _ema_series(close, min(50, max(10, len(close) - 1)))
    c = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    if not all(math.isfinite(x) for x in (c, e20, e50)):
        return None

    if c > e20 and e20 > e50:
        bias = "BUY"
        align = 1.0
    elif c < e20 and e20 < e50:
        bias = "SELL"
        align = 1.0
    elif c > e20:
        bias = "BUY"
        align = 0.45
    elif c < e20:
        bias = "SELL"
        align = 0.45
    else:
        return None

    n = min(consistency_bars, len(close))
    tail_c = close.iloc[-n:]
    tail_o = open_.iloc[-n:]
    if bias == "BUY":
        cons = float((tail_c > tail_o).sum()) / n
    else:
        cons = float((tail_c < tail_o).sum()) / n

    net = abs(float(tail_c.iloc[-1]) - float(tail_c.iloc[0]))
    path = float(tail_c.diff().abs().sum())
    efficiency = (net / path) if path > 1e-9 else 0.0
    efficiency = max(0.0, min(1.0, efficiency))

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.tail(n).mean()) if len(tr.dropna()) else 0.0
    atr_pct = (atr / c * 100.0) if c > 0 else 0.0

    plus_di, minus_di, adx = _wilder_di_adx(df.loc[valid].reset_index(drop=True), 14)
    di_score = 0.0
    if plus_di is not None and minus_di is not None:
        if bias == "BUY" and plus_di > minus_di:
            di_score = 0.7
        elif bias == "SELL" and minus_di > plus_di:
            di_score = 0.7
        if adx is not None and adx >= MIN_ADX:
            di_score = min(1.0, di_score + 0.3)
        elif adx is not None and adx < MIN_ADX * 0.7:
            di_score *= 0.4

    if cons < 0.50:
        return None

    return {
        "bias": bias,
        "align": align,
        "consistency": cons,
        "efficiency": efficiency,
        "di_score": di_score,
        "atr_pct": round(atr_pct, 4),
        "plus_di": plus_di,
        "minus_di": minus_di,
        "adx": adx,
        "ema20": round(e20, 4),
        "ema50": round(e50, 4),
        "close": round(c, 4),
    }


def normalize(rows, key):
    values = [num(x.get(key)) for x in rows]
    if not values:
        return
    lo, hi = min(values), max(values)
    for row in rows:
        v = num(row.get(key))
        row[key + "_norm"] = (v - lo) / (hi - lo) if hi > lo else 0.0


def parse_args():
    p = argparse.ArgumentParser(description="Matmon directional-persistence pre-open")
    p.add_argument("--write", action="store_true")
    p.add_argument("--allow-live-config", action="store_true")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--history-pool", type=int, default=DEFAULT_HISTORY_POOL)
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--consistency-bars", type=int, default=DEFAULT_CONSISTENCY_BARS)
    p.add_argument("--config", type=Path, default=CONFIG)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)

    top_n = max(10, int(args.top))
    history_pool = max(top_n, int(args.history_pool))
    lookback = max(8, int(args.lookback_days))
    consistency_bars = max(5, int(args.consistency_bars))

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("ABORT: user_config.json is not an object")

    paper = cfg.get("paper_trading") is True
    if args.write and not paper and not args.allow_live_config:
        raise SystemExit(
            "ABORT: --write on non-paper config requires --allow-live-config"
        )

    print("Connecting…")
    kite = get_kite_client()

    rows, cleaning = cleaned_equity_instruments(kite)
    print(f"Clean universe: {len(rows)}")
    if len(rows) < top_n:
        raise SystemExit(f"ABORT: clean universe only {len(rows)}")

    keys = [f'{r["exchange"]}:{r["symbol"]}' for r in rows]
    print(f"Fetching quotes for {len(keys)} symbols…")
    quotes = fetch_selector_quotes(kite, keys)
    print(f"Quotes received: {len(quotes)}")

    gated = []
    no_price = 0
    for row in rows:
        key = f'{row["exchange"]}:{row["symbol"]}'
        cand = quote_gate(row, quotes.get(key))
        if cand is None:
            no_price += 1
            continue
        if not cand.get("instrument_token"):
            try:
                cand["instrument_token"] = get_instrument_token(
                    kite, cand["symbol"], cand["exchange"]
                )
            except Exception:
                continue
        gated.append(cand)

    print(f"Passed quote gates: {len(gated)} (no usable price: {no_price})")
    if len(gated) < top_n:
        raise SystemExit(f"ABORT: only {len(gated)} gated candidates")

    gated.sort(key=lambda x: (-x["_liq_raw"], x["spread_pct"], x["symbol"]))
    pool = gated[:history_pool]
    print(f"History pool: {len(pool)}")

    scored = []
    failures = 0
    t0 = time.time()
    for i, cand in enumerate(pool, 1):
        token = int(cand["instrument_token"])
        try:
            df = fetch_candles(
                kite,
                token,
                interval="day",
                lookback_days=lookback + 5,
                trim_incomplete=True,
            )
        except Exception:
            failures += 1
            continue

        feats = directional_features(df, consistency_bars)
        if feats is None:
            failures += 1
            continue

        cand = dict(cand)
        cand.update(feats)
        scored.append(cand)

        if i % 50 == 0:
            elapsed = time.time() - t0
            print(
                f"  history {i}/{len(pool)} scored={len(scored)} "
                f"fail={failures} ({elapsed:.0f}s)"
            )

    print(f"Directional candidates: {len(scored)} (fail/skip={failures})")
    if len(scored) < top_n:
        raise SystemExit(
            f"ABORT: only {len(scored)} directional candidates; need {top_n}"
        )

    atrs = [num(x.get("atr_pct")) for x in scored if num(x.get("atr_pct")) > 0]
    atr_med = sorted(atrs)[len(atrs) // 2] if atrs else 0.0
    atr_cap = atr_med * ATR_TAIL_MULTIPLIER if atr_med > 0 else 1e9
    filtered = [x for x in scored if num(x.get("atr_pct")) <= atr_cap]
    print(
        f"After ATR tail filter (med={atr_med:.3f}% cap={atr_cap:.3f}%): {len(filtered)}"
    )
    if len(filtered) < top_n:
        filtered = scored

    normalize(filtered, "_liq_raw")

    for c in filtered:
        c["directional_score"] = round(
            W_ALIGN * num(c.get("align"))
            + W_CONSISTENCY * num(c.get("consistency"))
            + W_EFFICIENCY * num(c.get("efficiency"))
            + W_DI * num(c.get("di_score"))
            + W_LIQ * num(c.get("_liq_raw_norm")),
            6,
        )

    filtered.sort(
        key=lambda x: (
            -num(x.get("directional_score")),
            -num(x.get("consistency")),
            -num(x.get("efficiency")),
            x["symbol"],
        )
    )

    selected = []
    seen = set()
    for c in filtered:
        if c["symbol"] in seen:
            continue
        seen.add(c["symbol"])
        selected.append(c)
        if len(selected) == top_n:
            break

    if len(selected) < top_n:
        raise SystemExit(f"ABORT: unique selected={len(selected)} < {top_n}")

    watchlist = [{"symbol": x["symbol"], "exchange": x["exchange"]} for x in selected]
    shortlist_n = int(getattr(strategy_def, "MATMON_WATCHLIST_SIZE", 30))
    shortlist = watchlist[:shortlist_n]

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "strategy": "MATMON_DIRECTIONAL_PERSISTENCE",
        "cleaning": cleaning,
        "gated": len(gated),
        "history_pool": len(pool),
        "directional_scored": len(scored),
        "selected_count": len(selected),
        "shortlist_n": shortlist_n,
        "weights": {
            "align": W_ALIGN,
            "consistency": W_CONSISTENCY,
            "efficiency": W_EFFICIENCY,
            "di": W_DI,
            "liquidity": W_LIQ,
        },
        "atr_median_pct": atr_med,
        "atr_cap_pct": atr_cap,
        "watchlist": watchlist,
        "shortlist": shortlist,
        "selected_details": [
            {k: v for k, v in x.items() if not str(k).startswith("_")}
            for x in selected
        ],
    }

    report_path = RUNTIME / f"directional_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (RUNTIME / "latest.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    config_backup = None
    if args.write:
        backup_dir = RUNTIME / "config_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        config_backup = backup_dir / f"user_config_before_directional_{stamp}.json"
        shutil.copy2(args.config, config_backup)

        updated = dict(cfg)
        updated["watchlist"] = shortlist
        tmp = args.config.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        tmp.replace(args.config)

        check = json.loads(args.config.read_text(encoding="utf-8"))
        if check.get("watchlist") != shortlist:
            raise RuntimeError("POST-WRITE WATCHLIST CHECK FAILED")
        report["config_backup"] = str(config_backup)
        report["wrote_watchlist_count"] = len(shortlist)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (RUNTIME / "latest.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

    print()
    print("===== MATMON DIRECTIONAL PERSISTENCE =====")
    print(f"Selected Top-{top_n}: {len(selected)}")
    print(f"Live shortlist size: {shortlist_n}")
    print(f"BUY bias:  {sum(1 for x in selected if x.get('bias') == 'BUY')}")
    print(f"SELL bias: {sum(1 for x in selected if x.get('bias') == 'SELL')}")
    print(f"Report: {report_path}")
    if config_backup:
        print(f"Config backup: {config_backup}")
        print(f"WROTE watchlist n={len(shortlist)}")
    else:
        print("SHADOW only (no config write)")

    print()
    print("TOP 20")
    for i, x in enumerate(selected[:20], 1):
        print(
            f"{i:3d}. {x['exchange']}:{x['symbol']:<12} "
            f"bias={x.get('bias', '?'):<4} "
            f"score={x.get('directional_score', 0):.3f} "
            f"cons={x.get('consistency', 0):.2f} "
            f"eff={x.get('efficiency', 0):.2f} "
            f"di={x.get('di_score', 0):.2f} "
            f"atr%={x.get('atr_pct', 0):.2f}"
        )

    print()
    print("MATMON_DIRECTIONAL_PREOPEN=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
